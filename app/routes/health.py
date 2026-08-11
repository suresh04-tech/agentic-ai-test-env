"""Health, readiness, metrics, and the explicit database connectivity probe."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import check_connectivity, get_db, get_failure_state
from app.logging_config import get_logger
from app.metrics import registry
from app.routes._common import db_http_exception

router = APIRouter(tags=["observability"])
logger = get_logger(__name__)


@router.get("/health", summary="Liveness probe")
def health(settings: Settings = Depends(get_settings)) -> Response:
    """Return ``{"status": "healthy"}``.

    Database connectivity is *not* checked by default, so that a database
    outage shows up as failing API endpoints rather than as a dead container
    (which is what we want for RCA: the app is up, its dependency is not).
    Set ``HEALTH_CHECK_DB=true`` to include the database in this check.

    Deliberately exposes no configuration, environment variables, or
    connection details.
    """
    if not settings.health_check_db:
        return _json({"status": "healthy"})

    session_gen = get_db()
    session = next(session_gen)
    try:
        check_connectivity(session)
    except Exception:
        return _json(
            {"status": "unhealthy", "checks": {"database": "unreachable"}},
            status_code=503,
        )
    finally:
        session_gen.close()
    return _json({"status": "healthy", "checks": {"database": "ok"}})


@router.get("/ready", summary="Readiness probe (always checks the database)")
def ready(session: Session = Depends(get_db)) -> Response:
    try:
        check_connectivity(session)
    except Exception:
        return _json({"status": "not_ready", "database": "unreachable"}, status_code=503)
    return _json({"status": "ready", "database": "ok"})


@router.get("/metrics", summary="Prometheus scrape endpoint")
def metrics() -> Response:
    """Expose all registered metrics in Prometheus text format."""
    return Response(content=generate_latest(registry()), media_type=CONTENT_TYPE_LATEST)


@router.get("/api/db-check", summary="Database connectivity test")
def db_check(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Run a ``SELECT 1`` round-trip and report the result.

    Reports the target host:port (no credentials) so an operator can confirm
    *which* database was probed.
    """
    state = get_failure_state()
    try:
        check_connectivity(session)
    except Exception as exc:
        raise db_http_exception(exc, "connectivity_check") from exc

    logger.info(
        "Database connectivity check succeeded",
        extra={"operation": "connectivity_check", "status": "success",
               "db_target": settings.safe_database_target},
    )
    return _json(
        {
            "database": "reachable",
            "target": settings.safe_database_target,
            "failure_simulation_enabled": state.enabled,
        }
    )


def _json(payload: dict, status_code: int = 200) -> Response:
    return JSONResponse(content=payload, status_code=status_code)
