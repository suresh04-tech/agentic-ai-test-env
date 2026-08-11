"""Shared test configuration.

Unit tests never touch a real database. The environment below is applied
*before* the application package is imported, so ``get_settings()`` resolves to
an in-memory SQLite database and the app's normal startup path (create tables,
seed rows) runs against it.

Integration tests that need real PostgreSQL live in ``tests/integration`` and
are marked ``integration``.
"""

from __future__ import annotations

import os

# Must be set before importing app.config / app.main. Forced (not setdefault) so
# an exported DATABASE_URL can never point the unit suite at a real database.
# Integration tests use INTEGRATION_DATABASE_URL and their own engine instead.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
for _discrete in (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
):
    os.environ.pop(_discrete, None)
os.environ.setdefault("APP_NAME", "test-rca-app")
os.environ.setdefault("APP_ENV", "pytest")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("DB_AUTO_INIT", "true")
os.environ.setdefault("DB_SEED", "true")
os.environ.setdefault("DB_SEED_USERS", "5")
os.environ.setdefault("SIMULATE_DB_FAILURE", "false")
os.environ.setdefault("ENABLE_TEST_CONTROLS", "true")
os.environ.setdefault("CPU_STRESS_MAX_DURATION", "5")
os.environ.setdefault("SLOW_QUERY_MAX_SECONDS", "3")
os.environ.setdefault("SLOW_QUERY_DEFAULT_SECONDS", "1")
# The generator would issue real HTTP calls against a port nothing is listening
# on during tests.
os.environ.setdefault("TRAFFIC_GENERATOR_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import set_db_failure  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    """App client with lifespan run once (tables created, rows seeded).

    ``raise_server_exceptions=False`` so /api/error yields a real 500 response
    instead of propagating into the test, which is what we want to assert on.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_db_failure_switch():
    """Guarantee the failure simulation is disarmed around every test."""
    set_db_failure(False, "connection_refused")
    yield
    set_db_failure(False, "connection_refused")


def metric_value(metrics_text: str, sample_name: str) -> float:
    """Sum every sample line whose name+labels start with ``sample_name``."""
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.startswith(sample_name):
            try:
                total += float(line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                continue
    return total
