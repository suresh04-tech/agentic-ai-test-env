"""Optional background traffic generator.

An RCA test environment is useless without a baseline: Prometheus needs a
steady request rate to make an anomaly visible, and Loki needs a normal log
stream to contrast against. This generates that baseline by calling the app's
own HTTP endpoints, so requests pass through the real middleware and produce
real metrics and logs.

It only ever calls read endpoints plus one deliberate 404. It never triggers the
failure scenarios — those stay operator-driven.

Disabled unless ``TRAFFIC_GENERATOR_ENABLED=true``.
"""

from __future__ import annotations

import asyncio
import random

import httpx

from app.config import Settings
from app.logging_config import get_logger

logger = get_logger(__name__)

# (path, weight) — weights approximate a normal read-heavy workload.
TRAFFIC_MIX: tuple[tuple[str, int], ...] = (
    ("/api/users", 30),
    ("/api/users?limit=10", 15),
    ("/api/orders", 30),
    ("/api/orders?status=paid", 10),
    ("/api/orders?limit=5", 8),
    ("/health", 5),
    ("/api/db-check", 1),
    ("/api/does-not-exist", 1),  # a little 404 noise, as in real traffic
)

_PATHS = [path for path, _ in TRAFFIC_MIX]
_WEIGHTS = [weight for _, weight in TRAFFIC_MIX]


async def run_traffic_generator(settings: Settings) -> None:
    """Loop until cancelled, issuing roughly ``TRAFFIC_GENERATOR_RPS`` req/s."""
    interval = 1.0 / settings.traffic_generator_rps
    rng = random.Random(20260811)
    logger.info(
        "Traffic generator started",
        extra={
            "operation": "traffic_generator",
            "detail": f"rps={settings.traffic_generator_rps} "
            f"target={settings.traffic_generator_base_url}",
        },
    )

    # Give Uvicorn a moment to bind the socket before the first request.
    await asyncio.sleep(2)

    try:
        async with httpx.AsyncClient(
            base_url=settings.traffic_generator_base_url,
            timeout=httpx.Timeout(30.0),
            headers={"user-agent": "test-rca-app-traffic-generator/1.0"},
        ) as client:
            while True:
                path = rng.choices(_PATHS, weights=_WEIGHTS, k=1)[0]
                try:
                    await client.get(path)
                except httpx.HTTPError as exc:
                    # Self-calls failing is itself a useful signal, but must not
                    # kill the generator.
                    logger.warning(
                        "Traffic generator request failed",
                        extra={
                            "operation": "traffic_generator",
                            "error_type": type(exc).__name__,
                            "detail": str(exc)[:200],
                        },
                    )
                # Jitter so requests do not land in lockstep with the scrape.
                await asyncio.sleep(interval * rng.uniform(0.6, 1.4))
    except asyncio.CancelledError:
        logger.info(
            "Traffic generator stopped", extra={"operation": "traffic_generator"}
        )
        raise
