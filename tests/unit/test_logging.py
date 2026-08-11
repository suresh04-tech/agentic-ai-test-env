"""Structured logging: JSON shape, field whitelist, and secret redaction."""

from __future__ import annotations

import json
import logging

from app.logging_config import JsonFormatter, request_id_var


def _record(level: int = logging.INFO, message: str = "hello", **extra):
    record = logging.LogRecord(
        name="app.test",
        level=level,
        pathname=__file__,
        lineno=10,
        msg=message,
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _format(record) -> dict:
    formatter = JsonFormatter("test-rca-app", "pytest", "1.0.0")
    return json.loads(formatter.format(record))


def test_log_is_single_line_json_with_base_fields():
    formatter = JsonFormatter("test-rca-app", "pytest", "1.0.0")
    rendered = formatter.format(_record())
    assert "\n" not in rendered

    payload = json.loads(rendered)
    assert payload["level"] == "INFO"
    assert payload["service"] == "test-rca-app"
    assert payload["environment"] == "pytest"
    assert payload["version"] == "1.0.0"
    assert payload["message"] == "hello"
    assert payload["timestamp"].endswith("+00:00")


def test_context_fields_are_included():
    payload = _format(
        _record(
            endpoint="/api/users",
            method="GET",
            status=200,
            duration_ms=12.5,
            operation="select_users",
            rows=5,
        )
    )
    assert payload["endpoint"] == "/api/users"
    assert payload["method"] == "GET"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 12.5
    assert payload["operation"] == "select_users"
    assert payload["rows"] == 5


def test_secrets_are_redacted_even_if_passed_by_mistake():
    payload = _format(
        _record(password="hunter2", database_url="postgresql://u:p@h:5432/d", token="t")
    )
    assert payload["password"] == "***"
    assert payload["database_url"] == "***"
    assert payload["token"] == "***"
    assert "hunter2" not in json.dumps(payload)
    assert "5432" not in json.dumps(payload)


def test_unknown_extra_fields_are_dropped():
    payload = _format(_record(some_random_object=object()))
    assert "some_random_object" not in payload


def test_exception_records_carry_type_and_stack_trace():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record(level=logging.ERROR, message="failed")
        record.exc_info = sys.exc_info()

    payload = _format(record)
    assert payload["error_type"] == "ValueError"
    assert payload["error_message"] == "boom"
    assert "ValueError: boom" in payload["stack_trace"]


def test_request_id_is_attached_when_set():
    token = request_id_var.set("deadbeef")
    try:
        payload = _format(_record())
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "deadbeef"


def test_request_logs_carry_endpoint_and_status(client, caplog):
    with caplog.at_level("INFO"):
        client.get("/api/users")

    records = [
        record
        for record in caplog.records
        if getattr(record, "operation", None) == "http_request"
    ]
    assert records
    record = records[-1]
    assert record.endpoint == "/api/users"
    assert record.method == "GET"
    assert record.status == 200
    assert record.duration_ms >= 0
