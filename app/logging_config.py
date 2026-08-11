"""Structured JSON logging to stdout.

Logs go to stdout only, so the Docker json-file driver captures them and
Grafana Alloy can ship them to Loki. Nothing is written to local log files.

Every record carries a stable set of fields (timestamp, level, service,
environment, message) plus whatever context the call site attaches via
``extra={...}``. Field names are kept consistent across the app so Loki/LogQL
queries stay simple, e.g.:

    {container="test-rca-app"} | json | level="ERROR" | operation="database_query"
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Fields we allow through from ``extra=``. Anything else is dropped so a stray
# object never ends up serialised into the log stream.
CONTEXT_FIELDS = (
    "endpoint",
    "method",
    "status",
    "duration_ms",
    "operation",
    "error_type",
    "scenario",
    "rows",
    "db_target",
    "requested_duration_s",
    "actual_duration_s",
    "failure_mode",
    "enabled",
    "client_ip",
    "user_agent",
    "query",
    "detail",
)

# Never serialise these, whatever happens. Defence in depth: the app does not
# put secrets in log context in the first place.
REDACTED_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "database_url",
        "dsn",
        "credentials",
    }
)

_LOG_RECORD_BUILTINS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {"message", "asctime", "taskName"}

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    """Render every log record as a single-line JSON object."""

    def __init__(self, service: str, environment: str, version: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "environment": self.environment,
            "version": self.version,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id

        for key, value in vars(record).items():
            if key in _LOG_RECORD_BUILTINS or key.startswith("_"):
                continue
            if key.lower() in REDACTED_KEYS:
                payload[key] = "***"
                continue
            if key in CONTEXT_FIELDS:
                payload[key] = value

        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            payload.setdefault(
                "error_type", exc_type.__name__ if exc_type else "UnknownError"
            )
            payload["error_message"] = str(exc_value) if exc_value else ""
            payload["stack_trace"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    service: str = "test-rca-app",
    environment: str = "test",
    version: str = "1.0.0",
) -> None:
    """Install the JSON formatter on the root logger, writing to stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, environment, version))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Uvicorn ships its own handlers/formatters; strip them so everything the
    # server emits is JSON too, and drop the duplicate per-request access log
    # (our middleware emits a richer one).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
