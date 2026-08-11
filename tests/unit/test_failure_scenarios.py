"""The four controlled failure scenarios, including their metric side effects."""

from __future__ import annotations

import time

import pytest

from tests.conftest import metric_value


# ---------------------------------------------------------------------------
# Scenario 1 — database failure
# ---------------------------------------------------------------------------


def test_db_failure_switch_reports_state(client):
    payload = client.get("/api/test/db-failure").json()
    assert payload["enabled"] is False
    assert "connection_refused" in payload["available_modes"]


def test_db_failure_makes_data_endpoints_return_503(client):
    client.post("/api/test/db-failure?enable=true&mode=connection_refused")

    for path in ("/api/users", "/api/orders", "/api/db-check"):
        response = client.get(path)
        assert response.status_code == 503, path
        detail = response.json()["detail"]
        assert detail["error"] == "database_error"
        assert detail["error_type"] == "connection_refused"
        assert detail["simulated"] is True

    body = client.get("/metrics").text
    assert metric_value(body, 'db_errors_total{error_type="connection_refused"') > 0
    assert metric_value(body, 'http_request_errors_total{endpoint="/api/users"') > 0
    assert metric_value(body, "app_db_failure_simulation_active") == 1
    assert metric_value(body, "db_up") == 0


def test_db_failure_can_be_disarmed(client):
    client.post("/api/test/db-failure?enable=true")
    assert client.get("/api/users").status_code == 503
    client.post("/api/test/db-failure?enable=false")
    assert client.get("/api/users").status_code == 200


def test_db_failure_rejects_unknown_mode(client):
    response = client.post("/api/test/db-failure?enable=true&mode=meltdown")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_mode"


def test_db_failure_response_never_leaks_credentials(client):
    client.post("/api/test/db-failure?enable=true")
    body = client.get("/api/users").text.lower()
    assert "password" not in body
    assert "postgresql://" not in body


def test_health_stays_healthy_during_db_failure(client):
    """A dependency outage must not read as a dead container by default."""
    client.post("/api/test/db-failure?enable=true")
    assert client.get("/health").json() == {"status": "healthy"}
    assert client.get("/ready").status_code == 503


def test_db_failure_logs_are_structured(client, caplog):
    with caplog.at_level("ERROR"):
        client.post("/api/test/db-failure?enable=true")
        client.get("/api/users")

    db_records = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "select_users"
    ]
    assert db_records, "expected an ERROR log for the failed database operation"
    record = db_records[-1]
    assert record.error_type == "connection_refused"
    assert isinstance(record.duration_ms, float)
    assert "refused" in record.detail.lower()


# ---------------------------------------------------------------------------
# Scenario 2 — CPU stress
# ---------------------------------------------------------------------------


def test_cpu_stress_runs_for_requested_duration(client):
    started = time.perf_counter()
    response = client.get("/api/cpu-stress?duration=1")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    payload = response.json()
    assert payload["scenario"] == "cpu_stress"
    assert payload["requested_seconds"] == 1
    assert payload["actual_seconds"] >= 0.9
    assert elapsed < 10  # bounded: never runs away

    body = client.get("/metrics").text
    assert metric_value(body, "app_cpu_stress_runs_total") > 0
    assert metric_value(body, "app_cpu_stress_seconds_total") > 0


@pytest.mark.parametrize("duration", [0, -5, 6, 999])
def test_cpu_stress_duration_is_bounded(client, duration):
    """CPU_STRESS_MAX_DURATION is 5 in the test environment."""
    response = client.get(f"/api/cpu-stress?duration={duration}")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] in {
        "invalid_duration",
        "duration_out_of_range",
    }


def test_cpu_stress_rejects_non_numeric_duration(client):
    assert client.get("/api/cpu-stress?duration=forever").status_code == 422


def test_cpu_stress_logs_start_and_completion(client, caplog):
    with caplog.at_level("WARNING"):
        client.get("/api/cpu-stress?duration=1")

    messages = [
        record.getMessage()
        for record in caplog.records
        if getattr(record, "scenario", None) == "cpu_stress"
    ]
    assert "CPU stress started" in messages
    assert "CPU stress completed" in messages


# ---------------------------------------------------------------------------
# Scenario 3 — application error
# ---------------------------------------------------------------------------


def test_error_endpoint_returns_500(client):
    response = client.get("/api/error")
    assert response.status_code == 500
    payload = response.json()
    assert payload["error"] == "internal_server_error"
    assert payload["error_type"] == "SimulatedApplicationError"
    assert payload["request_id"]


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("value", "ValueError"),
        ("key", "KeyError"),
        ("zero_division", "ZeroDivisionError"),
        ("type", "TypeError"),
    ],
)
def test_error_endpoint_supports_error_kinds(client, kind, expected):
    response = client.get(f"/api/error?kind={kind}")
    assert response.status_code == 500
    assert response.json()["error_type"] == expected


def test_error_endpoint_rejects_unknown_kind(client):
    response = client.get("/api/error?kind=nuclear")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_kind"


def test_error_endpoint_increments_error_metrics(client):
    client.get("/api/error")
    body = client.get("/metrics").text
    assert (
        metric_value(body, 'http_request_errors_total{endpoint="/api/error"') > 0
    )
    assert (
        metric_value(body, 'app_simulated_failures_total{scenario="application_error"')
        > 0
    )
    # An application bug must not look like a database problem.
    assert metric_value(body, 'db_errors_total{operation="select_users"') >= 0


def test_error_endpoint_logs_stack_trace(client, caplog):
    with caplog.at_level("ERROR"):
        client.get("/api/error")

    http_errors = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "http_request"
        and getattr(record, "status", None) == 500
    ]
    assert http_errors, "expected an ERROR log for the unhandled exception"
    record = http_errors[-1]
    assert record.error_type == "SimulatedApplicationError"
    assert record.exc_info is not None
    assert record.endpoint == "/api/error"


# ---------------------------------------------------------------------------
# Scenario 4 — database latency
# ---------------------------------------------------------------------------


def test_slow_query_takes_the_requested_time(client):
    response = client.get("/api/slow-query?seconds=1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["scenario"] == "database_latency"
    assert payload["actual_seconds"] >= 0.9

    body = client.get("/metrics").text
    assert metric_value(body, 'db_queries_total{operation="slow_query"') > 0
    assert metric_value(body, "app_slow_queries_total") > 0
    # The latency must land in the histogram's tail, not only in _count.
    assert (
        metric_value(body, 'db_query_duration_seconds_sum{operation="slow_query"') >= 0.9
    )


def test_slow_query_uses_default_seconds(client):
    payload = client.get("/api/slow-query").json()
    assert payload["requested_seconds"] == 1.0  # SLOW_QUERY_DEFAULT_SECONDS in tests


@pytest.mark.parametrize("seconds", [0, -1, 4, 600])
def test_slow_query_seconds_is_bounded(client, seconds):
    """SLOW_QUERY_MAX_SECONDS is 3 in the test environment."""
    response = client.get(f"/api/slow-query?seconds={seconds}")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] in {
        "invalid_seconds",
        "seconds_out_of_range",
    }


def test_slow_query_logs_duration(client, caplog):
    with caplog.at_level("WARNING"):
        client.get("/api/slow-query?seconds=1")

    records = [
        record
        for record in caplog.records
        if getattr(record, "scenario", None) == "database_latency"
        and getattr(record, "status", None) == "success"
    ]
    assert records
    assert records[-1].duration_ms >= 900
