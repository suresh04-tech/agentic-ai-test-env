"""Shared helpers for turning database failures into HTTP responses."""

from __future__ import annotations

from fastapi import HTTPException

from app.database import SimulatedDatabaseFailure, classify_db_error

# Failures that mean "the dependency is down", not "the request was bad".
UNAVAILABLE_ERROR_TYPES = frozenset(
    {
        "connection_refused",
        "connection_timeout",
        "pool_timeout",
        "connection_limit",
        "operational_error",
        "query_error",
    }
)


def db_http_exception(exc: Exception, operation: str) -> HTTPException:
    """Map a database exception to an HTTPException with a safe message.

    The response body names the *class* of failure and the operation, never the
    host, user, password, or connection string. Full detail already went to the
    structured log inside ``tracked_db_operation``.
    """
    error_type = classify_db_error(exc)
    status_code = 503 if error_type in UNAVAILABLE_ERROR_TYPES else 500
    simulated = isinstance(exc, SimulatedDatabaseFailure)
    return HTTPException(
        status_code=status_code,
        detail={
            "error": "database_error",
            "error_type": error_type,
            "operation": operation,
            "simulated": simulated,
            "message": (
                "Database operation failed; the application could not serve this "
                "request. See application logs for details."
            ),
        },
    )
