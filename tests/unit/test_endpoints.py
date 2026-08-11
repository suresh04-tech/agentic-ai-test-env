"""Happy-path endpoint behaviour and metric exposure."""

from __future__ import annotations

from tests.conftest import metric_value


def test_health_returns_healthy(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_health_does_not_leak_configuration(client):
    body = client.get("/health").text.lower()
    for leak in ("password", "postgresql://", "sqlite", "database_url", "@"):
        assert leak not in body


def test_ready_checks_database(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}


def test_root_lists_endpoints(client):
    payload = client.get("/").json()
    assert payload["service"] == "test-rca-app"
    assert "/metrics" in payload["endpoints"]


def test_users_returns_seeded_rows(client):
    response = client.get("/api/users")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] > 0
    assert payload["count"] == len(payload["users"])
    first = payload["users"][0]
    assert set(first) == {"id", "name", "email", "created_at"}


def test_users_limit_is_validated(client):
    assert client.get("/api/users?limit=0").status_code == 422
    assert client.get("/api/users?limit=100000").status_code == 422


def test_orders_returns_rows(client):
    response = client.get("/api/orders")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == len(payload["orders"])
    if payload["orders"]:
        assert set(payload["orders"][0]) == {
            "id",
            "user_id",
            "amount",
            "status",
            "created_at",
        }


def test_orders_rejects_unknown_status(client):
    response = client.get("/api/orders?status=not-a-status")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_status"


def test_db_check_reports_reachable_without_credentials(client):
    response = client.get("/api/db-check")
    assert response.status_code == 200
    payload = response.json()
    assert payload["database"] == "reachable"
    assert "password" not in response.text.lower()


def test_metrics_endpoint_exposes_required_series(client):
    client.get("/api/users")
    client.get("/api/orders")
    body = client.get("/metrics").text

    for name in (
        "http_requests_total",
        "http_request_duration_seconds",
        "http_request_errors_total",
        "db_queries_total",
        "db_query_duration_seconds",
        "db_errors_total",
        "app_records_fetched_total",
        "app_info",
    ):
        assert name in body, f"missing metric: {name}"

    assert metric_value(body, 'http_requests_total{endpoint="/api/users"') > 0
    assert metric_value(body, 'db_queries_total{operation="select_users"') > 0


def test_metrics_endpoint_uses_route_templates_not_raw_paths(client):
    """Guards against label explosion: only route templates may appear."""
    client.get("/api/users?limit=3")
    body = client.get("/metrics").text
    assert 'endpoint="/api/users"' in body
    assert "limit=3" not in body


def test_metrics_leak_no_credentials(client):
    body = client.get("/metrics").text.lower()
    for leak in ("password", "postgresql://", "database_url", "sqlite+"):
        assert leak not in body


def test_request_id_header_is_returned(client):
    response = client.get("/health")
    assert response.headers.get("X-Request-ID")

    supplied = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert supplied.headers["X-Request-ID"] == "abc123"


def test_unmatched_path_collapses_to_single_label(client):
    client.get("/api/nope/12345")
    client.get("/api/nope/67890")
    body = client.get("/metrics").text
    assert 'endpoint="unmatched"' in body
    assert "12345" not in body
