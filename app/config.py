"""Environment-driven configuration.

Every value comes from the environment. Nothing about the database (host,
user, password, URL) is ever defaulted to a real value, and the resolved
connection string is never logged or exposed through an endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import quote_plus, urlsplit, urlunsplit


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _build_database_url() -> str:
    """Resolve DATABASE_URL, or assemble one from the discrete DB_* parts."""
    url = _env("DATABASE_URL")
    if url:
        return url

    host = _env("DATABASE_HOST")
    name = _env("DATABASE_NAME")
    user = _env("DATABASE_USER")
    password = _env("DATABASE_PASSWORD")
    port = _env("DATABASE_PORT", "5432")

    missing = [
        key
        for key, value in (
            ("DATABASE_HOST", host),
            ("DATABASE_NAME", name),
            ("DATABASE_USER", user),
            ("DATABASE_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "Database configuration is incomplete. Set DATABASE_URL, or all of "
            "DATABASE_HOST / DATABASE_PORT / DATABASE_NAME / DATABASE_USER / "
            f"DATABASE_PASSWORD. Missing: {', '.join(missing)}"
        )

    return (
        f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}"
    )


def redact_database_url(url: str) -> str:
    """Return a URL safe to log: credentials replaced with ``***``.

    Used only for diagnostics. The full URL is never emitted anywhere.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-database-url>"
    if not parts.hostname:
        return f"{parts.scheme}://<redacted>"
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        netloc = f"***:***@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@dataclass(frozen=True)
class Settings:
    """Immutable application settings snapshot."""

    app_name: str
    app_env: str
    app_version: str
    log_level: str
    port: int

    database_url: str = field(repr=False)
    db_pool_size: int
    db_max_overflow: int
    db_timeout: int
    db_statement_timeout_ms: int
    db_auto_init: bool
    db_seed: bool
    db_seed_users: int

    health_check_db: bool

    cpu_stress_max_duration: int
    slow_query_max_seconds: int
    slow_query_default_seconds: float

    simulate_db_failure: bool
    db_failure_mode: str
    db_failure_delay_seconds: float
    enable_test_controls: bool

    traffic_generator_enabled: bool
    traffic_generator_rps: float
    traffic_generator_base_url: str

    @property
    def safe_database_target(self) -> str:
        """Host/port only — never credentials."""
        try:
            parts = urlsplit(self.database_url)
            if parts.hostname:
                return f"{parts.hostname}:{parts.port or 5432}"
        except ValueError:
            pass
        return "unknown"


VALID_DB_FAILURE_MODES = ("connection_refused", "connection_timeout", "query_error")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    mode = (_env("DB_FAILURE_MODE", "connection_refused") or "").lower()
    if mode not in VALID_DB_FAILURE_MODES:
        raise ConfigError(
            f"DB_FAILURE_MODE must be one of {VALID_DB_FAILURE_MODES}, got {mode!r}"
        )

    port = _env_int("PORT", 8080)
    cpu_max = max(1, _env_int("CPU_STRESS_MAX_DURATION", 120))
    slow_max = max(1, _env_int("SLOW_QUERY_MAX_SECONDS", 30))

    return Settings(
        app_name=_env("APP_NAME", "test-rca-app") or "test-rca-app",
        app_env=_env("APP_ENV", "test") or "test",
        app_version=_env("APP_VERSION", "1.0.0") or "1.0.0",
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        port=port,
        database_url=_build_database_url(),
        db_pool_size=_env_int("DB_POOL_SIZE", 5),
        db_max_overflow=_env_int("DB_MAX_OVERFLOW", 5),
        db_timeout=_env_int("DB_TIMEOUT", 10),
        db_statement_timeout_ms=_env_int("DB_STATEMENT_TIMEOUT_MS", 60_000),
        db_auto_init=_env_bool("DB_AUTO_INIT", True),
        db_seed=_env_bool("DB_SEED", True),
        db_seed_users=max(1, _env_int("DB_SEED_USERS", 25)),
        health_check_db=_env_bool("HEALTH_CHECK_DB", False),
        cpu_stress_max_duration=cpu_max,
        slow_query_max_seconds=slow_max,
        slow_query_default_seconds=min(
            float(slow_max), _env_float("SLOW_QUERY_DEFAULT_SECONDS", 5.0)
        ),
        simulate_db_failure=_env_bool("SIMULATE_DB_FAILURE", False),
        db_failure_mode=mode,
        db_failure_delay_seconds=max(
            0.0, _env_float("DB_FAILURE_DELAY_SECONDS", 2.0)
        ),
        enable_test_controls=_env_bool("ENABLE_TEST_CONTROLS", True),
        traffic_generator_enabled=_env_bool("TRAFFIC_GENERATOR_ENABLED", False),
        traffic_generator_rps=max(0.1, _env_float("TRAFFIC_GENERATOR_RPS", 2.0)),
        traffic_generator_base_url=(
            _env("TRAFFIC_GENERATOR_BASE_URL", f"http://127.0.0.1:{port}")
            or f"http://127.0.0.1:{port}"
        ),
    )
