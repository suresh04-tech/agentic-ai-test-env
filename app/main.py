"""FastAPI application factory and lifespan wiring.

Startup order matters for the RCA story: logging is configured before anything
else, so even a configuration failure is emitted as structured JSON that Alloy
can ship to Loki.

A database that is unreachable at startup does **not** stop the app. The
container stays up and its API endpoints fail with 503s — which is exactly the
signal an RCA agent should see for a dependency outage.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import dispose_engine, init_database, init_failure_state
from app.logging_config import configure_logging, get_logger, request_id_var
from app.metrics import set_app_info
from app.middleware import ObservabilityMiddleware
from app.routes import health, orders, test_failures, users
from app.traffic import run_traffic_generator

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "Application starting",
        extra={
            "operation": "startup",
            "db_target": settings.safe_database_target,
            "detail": (
                f"db_auto_init={settings.db_auto_init} "
                f"health_check_db={settings.health_check_db} "
                f"simulate_db_failure={settings.simulate_db_failure}"
            ),
        },
    )

    init_failure_state(settings)

    if settings.db_auto_init:
        try:
            init_database(seed=settings.db_seed, seed_users=settings.db_seed_users)
        except Exception:
            # Intentionally non-fatal: see module docstring.
            logger.error(
                "Database initialisation failed; starting anyway. "
                "Database-backed endpoints will return 503 until it recovers.",
                extra={
                    "operation": "startup_db_init",
                    "db_target": settings.safe_database_target,
                },
                exc_info=True,
            )

    traffic_task: asyncio.Task | None = None
    if settings.traffic_generator_enabled:
        traffic_task = asyncio.create_task(run_traffic_generator(settings))

    logger.info("Application ready", extra={"operation": "startup"})
    try:
        yield
    finally:
        if traffic_task is not None:
            traffic_task.cancel()
            try:
                await traffic_task
            except asyncio.CancelledError:
                pass
        dispose_engine()
        logger.info("Application stopped", extra={"operation": "shutdown"})


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service=settings.app_name,
        environment=settings.app_env,
        version=settings.app_version,
    )
    set_app_info(settings.app_name, settings.app_env, settings.app_version)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Controlled observability / RCA test application. Emits Prometheus "
            "metrics on /metrics and structured JSON logs to stdout, and exposes "
            "bounded, non-destructive failure scenarios."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(ObservabilityMiddleware)

    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(orders.router)
    app.include_router(test_failures.router)

    @app.get("/", tags=["observability"], summary="Service banner")
    def root() -> dict:
        return {
            "service": settings.app_name,
            "environment": settings.app_env,
            "version": settings.app_version,
            "endpoints": [
                "/health",
                "/ready",
                "/metrics",
                "/api/users",
                "/api/orders",
                "/api/db-check",
                "/api/slow-query",
                "/api/error",
                "/api/cpu-stress",
                "/api/test/db-failure",
                "/docs",
            ],
        }

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        """Render unhandled exceptions as JSON.

        The metrics and the stack-trace log are emitted by the middleware, which
        sees the exception before this handler does. The response body carries
        the exception class only — never configuration or connection details.

        This handler sits *outside* the middleware, so the request-id
        contextvar has already been reset by the time it runs; the id is read
        back off the scope instead.
        """
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "error_type": type(exc).__name__,
                "message": "The application failed to process this request.",
                "request_id": request.scope.get("request_id") or request_id_var.get(),
            },
        )

    return app


app = create_app()
