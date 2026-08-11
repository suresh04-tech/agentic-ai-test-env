"""Database engine, instrumented operations, and the controlled failure switch.

Three responsibilities live here:

1. Build the SQLAlchemy engine from environment configuration.
2. Wrap every database operation in ``tracked_db_operation`` so metrics and
   structured logs are emitted consistently — this is what makes the RCA story
   coherent (``db_query_duration_seconds`` and ``db_errors_total`` always move
   together with the matching log line).
3. Provide a *non-destructive* database-failure simulation. Nothing here drops
   tables, kills connections, or mutates the real database; the simulation
   raises synthetic connection errors in the application layer instead.
"""

from __future__ import annotations

import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError as SAPoolTimeoutError,
)
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.logging_config import get_logger
from app.metrics import (
    app_db_failure_simulation_active,
    app_simulated_failures_total,
    db_up,
    observe_db_error,
    observe_db_operation,
)
from app.models import ORDER_STATUSES, Base, Order, User

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Simulated failure (Scenario 1) — application-level only, never destructive
# ---------------------------------------------------------------------------


class SimulatedDatabaseFailure(Exception):
    """A synthetic database failure produced by the test control switch.

    Carries an ``error_type`` that mirrors the vocabulary a real driver failure
    would produce, so log/metric queries look the same either way.
    """

    def __init__(self, message: str, error_type: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass
class DbFailureState:
    enabled: bool
    mode: str


_failure_state = DbFailureState(enabled=False, mode="connection_refused")


def init_failure_state(settings: Settings) -> None:
    _failure_state.enabled = settings.simulate_db_failure
    _failure_state.mode = settings.db_failure_mode
    app_db_failure_simulation_active.set(1 if _failure_state.enabled else 0)


def get_failure_state() -> DbFailureState:
    return _failure_state


def set_db_failure(enabled: bool, mode: str | None = None) -> DbFailureState:
    """Toggle the simulation at runtime (used by the test-control endpoint)."""
    _failure_state.enabled = enabled
    if mode:
        _failure_state.mode = mode
    app_db_failure_simulation_active.set(1 if enabled else 0)
    return _failure_state


_FAILURE_MESSAGES = {
    "connection_refused": (
        'could not connect to server: Connection refused. Is the server running on host "{target}" '
        "and accepting TCP/IP connections on that port?"
    ),
    "connection_timeout": (
        'timeout expired while connecting to database host "{target}" '
        "(connect_timeout exceeded)"
    ),
    "query_error": (
        'query failed on database host "{target}": server closed the connection unexpectedly '
        "while executing statement"
    ),
}


def _maybe_fail(operation: str, settings: Settings) -> None:
    """Raise a synthetic failure if the simulation is armed."""
    if not _failure_state.enabled:
        return

    mode = _failure_state.mode
    if mode == "connection_timeout" and settings.db_failure_delay_seconds > 0:
        # Real connect timeouts burn latency before failing; reproduce that so
        # request-duration histograms shift the way they would in production.
        time.sleep(settings.db_failure_delay_seconds)

    db_up.set(0)
    app_simulated_failures_total.labels(scenario="database_failure").inc()
    message = _FAILURE_MESSAGES[mode].format(target=settings.safe_database_target)
    raise SimulatedDatabaseFailure(message, error_type=mode)


# ---------------------------------------------------------------------------
# Error classification — keeps ``error_type`` a small, bounded label set
# ---------------------------------------------------------------------------


def classify_db_error(exc: BaseException) -> str:
    if isinstance(exc, SimulatedDatabaseFailure):
        return exc.error_type
    if isinstance(exc, SAPoolTimeoutError):
        return "pool_timeout"
    if isinstance(exc, IntegrityError):
        return "integrity_error"
    if isinstance(exc, OperationalError):
        message = str(exc.orig or exc).lower()
        if "timeout" in message or "timed out" in message:
            return "connection_timeout"
        if "refused" in message:
            return "connection_refused"
        if "does not exist" in message or "authentication" in message:
            return "authentication_error"
        if "too many clients" in message:
            return "connection_limit"
        return "operational_error"
    if isinstance(exc, DBAPIError):
        return "dbapi_error"
    if isinstance(exc, SQLAlchemyError):
        return type(exc).__name__
    return type(exc).__name__


# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------


def _normalise_url(url: str) -> str:
    """Pin the psycopg3 driver for bare PostgreSQL URLs."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    url = _normalise_url(settings.database_url)

    if url.startswith("sqlite"):
        # Only reached by unit tests, which run against an in-memory database.
        engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_timeout,
            pool_recycle=1800,
            connect_args={
                "connect_timeout": settings.db_timeout,
                "options": f"-c statement_timeout={settings.db_statement_timeout_ms}",
            },
        )

    logger.info(
        "Database engine initialised",
        extra={
            "operation": "engine_init",
            "db_target": settings.safe_database_target,
        },
    )
    return engine


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------


@contextmanager
def tracked_db_operation(operation: str, slow_threshold_seconds: float = 1.0):
    """Time a database operation, emit metrics, and log the outcome.

    On success: ``db_queries_total{status="success"}`` and
    ``db_query_duration_seconds`` are updated; operations slower than
    ``slow_threshold_seconds`` are logged at WARNING with ``duration_ms`` so
    latency incidents are visible in Loki, not only in Prometheus.

    On failure: ``db_queries_total{status="error"}`` and ``db_errors_total`` are
    updated and an ERROR log with ``error_type`` is emitted. The exception is
    re-raised for the caller to translate into an HTTP response.
    """
    settings = get_settings()
    start = time.perf_counter()
    try:
        _maybe_fail(operation, settings)
        yield
    except Exception as exc:  # noqa: BLE001 - re-raised below
        duration = time.perf_counter() - start
        error_type = classify_db_error(exc)
        observe_db_operation(operation, duration, "error")
        observe_db_error(operation, error_type)
        if error_type in {"connection_refused", "connection_timeout", "pool_timeout"}:
            db_up.set(0)
        logger.error(
            "Database operation failed",
            extra={
                "operation": operation,
                "status": "error",
                "error_type": error_type,
                "duration_ms": round(duration * 1000, 2),
                "db_target": settings.safe_database_target,
                "detail": str(exc)[:500],
            },
            exc_info=not isinstance(exc, SimulatedDatabaseFailure),
        )
        raise
    else:
        duration = time.perf_counter() - start
        observe_db_operation(operation, duration, "success")
        db_up.set(1)
        level = logger.warning if duration >= slow_threshold_seconds else logger.debug
        level(
            "Slow database operation" if duration >= slow_threshold_seconds
            else "Database operation completed",
            extra={
                "operation": operation,
                "status": "success",
                "duration_ms": round(duration * 1000, 2),
                "db_target": settings.safe_database_target,
            },
        )


# ---------------------------------------------------------------------------
# Operations used by the routes
# ---------------------------------------------------------------------------


def check_connectivity(session: Session) -> None:
    """``SELECT 1`` round-trip. Raises on failure."""
    with tracked_db_operation("connectivity_check"):
        session.execute(text("SELECT 1")).scalar_one()


def fetch_users(session: Session, limit: int) -> list[User]:
    with tracked_db_operation("select_users"):
        return list(
            session.execute(select(User).order_by(User.id).limit(limit)).scalars()
        )


def fetch_orders(session: Session, limit: int, status: str | None = None) -> list[Order]:
    with tracked_db_operation("select_orders"):
        stmt = select(Order).order_by(Order.id.desc()).limit(limit)
        if status:
            stmt = stmt.where(Order.status == status)
        return list(session.execute(stmt).scalars())


def run_slow_query(session: Session, seconds: float) -> float:
    """Sleep inside the database for ``seconds``, then return.

    Uses ``pg_sleep`` on PostgreSQL — a read-only, self-terminating delay that
    holds one connection and nothing else. No locks are taken, so it cannot
    leave the database in a bad state.
    """
    dialect = session.bind.dialect.name if session.bind is not None else "unknown"
    with tracked_db_operation("slow_query", slow_threshold_seconds=0.5):
        if dialect == "postgresql":
            session.execute(text("SELECT pg_sleep(:seconds)"), {"seconds": seconds})
        else:
            # SQLite (unit tests) has no server-side sleep.
            time.sleep(seconds)
            session.execute(text("SELECT 1")).scalar_one()
    return seconds


# ---------------------------------------------------------------------------
# Schema init + seed
# ---------------------------------------------------------------------------

_SEED_FIRST_NAMES = (
    "Asha", "Ravi", "Meera", "Vikram", "Nikhil", "Priya", "Arun", "Kavya",
    "Sanjay", "Divya", "Rahul", "Anita", "Karthik", "Sneha", "Vishal",
)
_SEED_LAST_NAMES = (
    "Iyer", "Sharma", "Nair", "Reddy", "Gupta", "Menon", "Rao", "Patel",
)


def init_database(seed: bool = True, seed_users: int = 25) -> None:
    """Create tables if absent and optionally insert a small seed dataset.

    Idempotent: safe to run on every container start.
    """
    engine = get_engine()
    with tracked_db_operation("schema_init", slow_threshold_seconds=5.0):
        Base.metadata.create_all(bind=engine)

    if not seed:
        return

    session_factory = get_session_factory()
    with session_factory() as session:
        with tracked_db_operation("seed_check"):
            existing = session.execute(select(func.count(User.id))).scalar_one()
        if existing:
            logger.info(
                "Seed data already present; skipping",
                extra={"operation": "seed", "rows": int(existing)},
            )
            return

        rng = random.Random(1337)  # deterministic seed data across restarts
        with tracked_db_operation("seed_insert", slow_threshold_seconds=5.0):
            users = []
            for index in range(seed_users):
                first = rng.choice(_SEED_FIRST_NAMES)
                last = rng.choice(_SEED_LAST_NAMES)
                users.append(
                    User(
                        name=f"{first} {last}",
                        email=f"{first.lower()}.{last.lower()}{index}@example.test",
                    )
                )
            session.add_all(users)
            session.flush()

            orders = []
            for user in users:
                for _ in range(rng.randint(0, 4)):
                    orders.append(
                        Order(
                            user_id=user.id,
                            amount=round(rng.uniform(9.99, 999.99), 2),
                            status=rng.choice(ORDER_STATUSES),
                        )
                    )
            session.add_all(orders)
            session.commit()

        logger.info(
            "Seed data inserted",
            extra={"operation": "seed", "rows": len(users) + len(orders)},
        )


def dispose_engine() -> None:
    """Close pooled connections on shutdown."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
