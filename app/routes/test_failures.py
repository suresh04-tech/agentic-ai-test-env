"""Controlled failure scenarios for RCA testing.

Every scenario here is bounded and non-destructive:

* ``/api/slow-query`` uses a read-only, self-terminating ``pg_sleep`` bounded by
  ``SLOW_QUERY_MAX_SECONDS``.
* ``/api/error`` raises an application exception; no state is touched.
* ``/api/cpu-stress`` burns CPU for a bounded duration on a worker thread, with
  a concurrency cap, and always stops on its own.
* ``/api/test/db-failure`` flips an in-process flag that makes database calls
  raise synthetic connection errors. It never touches the real database.

None of these endpoints delete data, drop tables, kill connections, or run
unbounded loops.
"""

from __future__ import annotations

import math
import os
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.database import (
    get_db,
    get_failure_state,
    run_slow_query,
    set_db_failure,
)
from app.logging_config import get_logger
from app.metrics import (
    app_cpu_stress_active,
    app_cpu_stress_runs_total,
    app_cpu_stress_seconds_total,
    app_simulated_failures_total,
    app_slow_queries_total,
)
from app.routes._common import db_http_exception

router = APIRouter(tags=["failure-scenarios"])
logger = get_logger(__name__)

# Bounded concurrency: never allow more simultaneous burners than the box has
# cores, so the container stays responsive and the scenario stays controlled.
_CPU_STRESS_SLOTS = max(1, os.cpu_count() or 1)
_cpu_stress_semaphore = threading.BoundedSemaphore(_CPU_STRESS_SLOTS)

APP_ERROR_KINDS = ("runtime", "value", "key", "zero_division", "type")


class SimulatedApplicationError(RuntimeError):
    """Raised by ``/api/error`` to represent an application-layer bug."""


# ---------------------------------------------------------------------------
# Scenario 4: database latency
# ---------------------------------------------------------------------------


@router.get("/api/slow-query", summary="Scenario 4 — controlled database latency")
def slow_query(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    seconds: float | None = Query(
        None,
        description="Seconds to sleep inside the database. "
        "Defaults to SLOW_QUERY_DEFAULT_SECONDS, capped at SLOW_QUERY_MAX_SECONDS.",
    ),
) -> dict:
    requested = settings.slow_query_default_seconds if seconds is None else seconds
    if requested <= 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_seconds", "message": "seconds must be > 0"},
        )
    if requested > settings.slow_query_max_seconds:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "seconds_out_of_range",
                "message": (
                    f"seconds must be <= {settings.slow_query_max_seconds} "
                    "(SLOW_QUERY_MAX_SECONDS)"
                ),
            },
        )

    logger.info(
        "Slow query starting",
        extra={
            "operation": "slow_query",
            "scenario": "database_latency",
            "requested_duration_s": requested,
        },
    )
    started = time.perf_counter()
    try:
        run_slow_query(session, requested)
    except Exception as exc:
        raise db_http_exception(exc, "slow_query") from exc

    actual = time.perf_counter() - started
    app_slow_queries_total.inc()
    logger.warning(
        "Slow database operation completed",
        extra={
            "operation": "slow_query",
            "scenario": "database_latency",
            "status": "success",
            "requested_duration_s": requested,
            "actual_duration_s": round(actual, 3),
            "duration_ms": round(actual * 1000, 2),
            "db_target": settings.safe_database_target,
        },
    )
    return {
        "scenario": "database_latency",
        "requested_seconds": requested,
        "actual_seconds": round(actual, 3),
        "note": "Database-side sleep completed; no locks were taken.",
    }


# ---------------------------------------------------------------------------
# Scenario 3: application error
# ---------------------------------------------------------------------------


def _raise_kind(kind: str) -> None:
    """Raise the requested error a couple of frames deep, for a real traceback."""
    if kind == "value":
        int("not-a-number")
    elif kind == "key":
        {"order": 1}["missing_key"]
    elif kind == "zero_division":
        1 / 0
    elif kind == "type":
        None + 1  # type: ignore[operator]
    else:
        raise SimulatedApplicationError(
            "Simulated application failure while processing order payload"
        )


@router.get("/api/error", summary="Scenario 3 — controlled application exception")
def application_error(
    kind: str = Query(
        "runtime",
        description=f"Error flavour to raise. One of: {', '.join(APP_ERROR_KINDS)}",
    ),
) -> dict:
    if kind not in APP_ERROR_KINDS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_kind",
                "message": f"kind must be one of: {', '.join(APP_ERROR_KINDS)}",
            },
        )

    app_simulated_failures_total.labels(scenario="application_error").inc()
    logger.error(
        "About to raise a simulated application exception",
        extra={
            "operation": "application_error",
            "scenario": "application_error",
            "error_type": kind,
        },
    )
    # Deliberately unhandled: the middleware records the 500 and logs the full
    # stack trace, and the global handler renders the JSON body.
    _raise_kind(kind)
    return {"unreachable": True}


# ---------------------------------------------------------------------------
# Scenario 2: CPU stress
# ---------------------------------------------------------------------------


def _burn_cpu(duration_seconds: float) -> tuple[float, int]:
    """Busy-loop for at most ``duration_seconds``. Always terminates.

    The deadline is checked every iteration batch, so this cannot become an
    infinite loop even if the clock jumps.
    """
    deadline = time.monotonic() + duration_seconds
    started = time.perf_counter()
    iterations = 0
    while time.monotonic() < deadline:
        for value in range(5_000):
            math.sqrt(value * 3.1415926) * math.sin(value)
        iterations += 1
    return time.perf_counter() - started, iterations


@router.get("/api/cpu-stress", summary="Scenario 2 — bounded CPU stress")
async def cpu_stress(
    settings: Settings = Depends(get_settings),
    duration: int = Query(10, description="Seconds of CPU load to generate."),
) -> dict:
    if duration <= 0:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_duration", "message": "duration must be > 0"},
        )
    if duration > settings.cpu_stress_max_duration:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "duration_out_of_range",
                "message": (
                    f"duration must be <= {settings.cpu_stress_max_duration} seconds "
                    "(CPU_STRESS_MAX_DURATION)"
                ),
            },
        )

    if not _cpu_stress_semaphore.acquire(blocking=False):
        logger.warning(
            "CPU stress rejected: all stress slots busy",
            extra={
                "operation": "cpu_stress",
                "scenario": "cpu_stress",
                "detail": f"max_concurrent={_CPU_STRESS_SLOTS}",
            },
        )
        raise HTTPException(
            status_code=429,
            detail={
                "error": "cpu_stress_busy",
                "message": (
                    f"At most {_CPU_STRESS_SLOTS} concurrent CPU stress runs are "
                    "allowed; try again shortly."
                ),
            },
        )

    app_cpu_stress_active.inc()
    logger.warning(
        "CPU stress started",
        extra={
            "operation": "cpu_stress",
            "scenario": "cpu_stress",
            "requested_duration_s": duration,
        },
    )
    try:
        actual, iterations = await run_in_threadpool(_burn_cpu, float(duration))
    finally:
        app_cpu_stress_active.dec()
        _cpu_stress_semaphore.release()

    app_cpu_stress_runs_total.inc()
    app_cpu_stress_seconds_total.inc(actual)
    logger.warning(
        "CPU stress completed",
        extra={
            "operation": "cpu_stress",
            "scenario": "cpu_stress",
            "status": "success",
            "requested_duration_s": duration,
            "actual_duration_s": round(actual, 3),
            "duration_ms": round(actual * 1000, 2),
        },
    )
    return {
        "scenario": "cpu_stress",
        "requested_seconds": duration,
        "actual_seconds": round(actual, 3),
        "iterations": iterations,
        "max_allowed_seconds": settings.cpu_stress_max_duration,
    }


# ---------------------------------------------------------------------------
# Scenario 1: database failure switch
# ---------------------------------------------------------------------------


@router.get("/api/test/db-failure", summary="Scenario 1 — read the failure switch")
def read_db_failure_state(settings: Settings = Depends(get_settings)) -> dict:
    state = get_failure_state()
    return {
        "enabled": state.enabled,
        "mode": state.mode,
        "controls_enabled": settings.enable_test_controls,
        "available_modes": ["connection_refused", "connection_timeout", "query_error"],
    }


@router.post("/api/test/db-failure", summary="Scenario 1 — arm/disarm the switch")
def toggle_db_failure(
    settings: Settings = Depends(get_settings),
    enable: bool = Query(..., description="true to simulate database failure"),
    mode: str | None = Query(
        None,
        description="connection_refused | connection_timeout | query_error",
    ),
) -> dict:
    if not settings.enable_test_controls:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "test_controls_disabled",
                "message": "Set ENABLE_TEST_CONTROLS=true to use this endpoint.",
            },
        )
    valid_modes = ("connection_refused", "connection_timeout", "query_error")
    if mode is not None and mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_mode",
                "message": f"mode must be one of: {', '.join(valid_modes)}",
            },
        )

    state = set_db_failure(enable, mode)
    if enable:
        app_simulated_failures_total.labels(scenario="database_failure_armed").inc()
    logger.warning(
        "Database failure simulation %s" % ("ENABLED" if enable else "DISABLED"),
        extra={
            "operation": "db_failure_simulation",
            "scenario": "database_failure",
            "enabled": state.enabled,
            "failure_mode": state.mode,
        },
    )
    return {
        "enabled": state.enabled,
        "mode": state.mode,
        "note": (
            "Simulation is application-level only; the real database was not "
            "modified. Database-backed endpoints will now fail."
        ),
    }
