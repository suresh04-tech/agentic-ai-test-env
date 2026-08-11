"""Request instrumentation middleware.

One place records, for every request: the HTTP metrics, the access log, and a
correlation id. Handlers therefore only need to log domain detail.

The ``endpoint`` label is the matched *route template* (``/api/orders``), never
the raw path, so a path with an id in it can never explode Prometheus
cardinality. Unmatched paths collapse to ``unmatched``.
"""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.logging_config import get_logger, request_id_var
from app.metrics import (
    http_request_duration_seconds,
    http_request_errors_total,
    http_requests_in_progress,
    http_requests_total,
)

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def _endpoint_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    return "unmatched"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, slow_request_threshold_seconds: float = 1.0) -> None:
        super().__init__(app)
        self.slow_request_threshold = slow_request_threshold_seconds

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        # Also stashed on the scope: exception handlers installed above this
        # middleware run after the contextvar has been reset.
        request.scope["request_id"] = request_id
        method = request.method
        http_requests_in_progress.labels(method=method).inc()
        start = time.perf_counter()

        try:
            response: Response = await call_next(request)
        except Exception as exc:  # unhandled handler exception -> 500
            duration = time.perf_counter() - start
            endpoint = _endpoint_label(request)
            http_requests_total.labels(
                method=method, endpoint=endpoint, status="500"
            ).inc()
            http_request_errors_total.labels(
                method=method, endpoint=endpoint, status="500"
            ).inc()
            http_request_duration_seconds.labels(
                method=method, endpoint=endpoint
            ).observe(duration)
            logger.error(
                "Unhandled exception while processing request",
                extra={
                    "endpoint": endpoint,
                    "method": method,
                    "status": 500,
                    "duration_ms": round(duration * 1000, 2),
                    "operation": "http_request",
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            raise
        else:
            duration = time.perf_counter() - start
            endpoint = _endpoint_label(request)
            status = str(response.status_code)
            http_requests_total.labels(
                method=method, endpoint=endpoint, status=status
            ).inc()
            http_request_duration_seconds.labels(
                method=method, endpoint=endpoint
            ).observe(duration)
            if response.status_code >= 400:
                http_request_errors_total.labels(
                    method=method, endpoint=endpoint, status=status
                ).inc()

            response.headers[REQUEST_ID_HEADER] = request_id

            if response.status_code >= 500:
                log = logger.error
                message = "Request failed with server error"
            elif response.status_code >= 400:
                log = logger.warning
                message = "Request failed with client error"
            elif duration >= self.slow_request_threshold:
                log = logger.warning
                message = "Slow request completed"
            else:
                log = logger.info
                message = "Request completed"

            log(
                message,
                extra={
                    "endpoint": endpoint,
                    "method": method,
                    "status": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "operation": "http_request",
                    "client_ip": request.client.host if request.client else None,
                    "user_agent": request.headers.get("user-agent", "")[:120],
                },
            )
            return response
        finally:
            http_requests_in_progress.labels(method=method).dec()
            request_id_var.reset(token)


def json_error(status_code: int, error: str, message: str, request_id: str | None = None):
    """Uniform error body. Never includes connection strings or credentials."""
    payload = {"error": error, "message": message}
    if request_id:
        payload["request_id"] = request_id
    return JSONResponse(status_code=status_code, content=payload)
