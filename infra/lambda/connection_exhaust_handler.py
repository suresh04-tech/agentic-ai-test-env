"""Lambda → RDS Connection Exhaustion — RCA Test Scenario.

Anti-pattern: opens a brand-new psycopg connection on every invocation and
holds it open for HOLD_SECONDS before returning.  Under concurrency this
quickly exhausts db.t3.micro's ~87-connection limit, producing:

  - RDS DatabaseConnections > threshold  (Alarm 3)
  - psycopg OperationalError: too many connections → Lambda Errors  (Alarm 1)
  - Lambda Duration p95 spikes as the connection attempt times out  (Alarm 2)
  - RDS CPU rises from connection management overhead  (Alarm 4)

This is deliberately NOT production-safe.  It is an isolated test function
that connects to a staging RDS instance and executes a single read-only
SELECT — no writes, no DDL, no data modification.

Environment variables (injected by Terraform):
  DATABASE_URL   – postgresql+psycopg://user:pass@host:5432/dbname
  HOLD_SECONDS   – seconds to hold the connection open (default: 8)
  LOG_LEVEL      – DEBUG | INFO (default: INFO)
"""

from __future__ import annotations

import json
import logging
import os
import time

# psycopg (v3) — installed via Lambda Layer / zip; no SQLAlchemy, no pooling.
import psycopg

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", '
           '"logger": "%(name)s", "message": %(message)s}',
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("conn_exhaust")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ["DATABASE_URL"]
HOLD_SECONDS: float = float(os.environ.get("HOLD_SECONDS", "8"))
CONNECT_TIMEOUT: int = int(os.environ.get("CONNECT_TIMEOUT", "10"))

# Strip the SQLAlchemy dialect prefix so psycopg can parse the URL directly.
_PSYCOPG_URL = (
    DATABASE_URL
    .replace("postgresql+psycopg://", "postgresql://")
    .replace("postgres+psycopg://", "postgresql://")
)


# ---------------------------------------------------------------------------
# Handler — anti-pattern: new connection per invocation
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    """Handle one API request by opening a fresh DB connection.

    INTENTIONAL ANTI-PATTERN: This function opens a new psycopg connection on
    every invocation.  There is no module-level connection reuse, no
    connection pooling, and no connection release before the sleep completes.

    Under concurrency:
        concurrent_lambdas × 1_connection_each → RDS connection limit hit
    """
    request_id = getattr(context, "aws_request_id", "local")
    started = time.perf_counter()

    logger.info(
        json.dumps({
            "event": "connection_attempt",
            "request_id": request_id,
            "hold_seconds": HOLD_SECONDS,
        })
    )

    conn = None
    try:
        # ── Anti-pattern: new connection every time ─────────────────────────
        conn = psycopg.connect(
            _PSYCOPG_URL,
            connect_timeout=CONNECT_TIMEOUT,
            # autocommit avoids an idle transaction holding locks
            autocommit=True,
        )
        connect_ms = round((time.perf_counter() - started) * 1000, 1)

        logger.info(
            json.dumps({
                "event": "connection_opened",
                "request_id": request_id,
                "connect_ms": connect_ms,
            })
        )

        # Execute a trivial, read-only query (no locks, no writes)
        with conn.cursor() as cur:
            cur.execute("SELECT version(), pg_backend_pid(), now()")
            row = cur.fetchone()
            db_version = row[0] if row else "unknown"
            backend_pid = row[1] if row else -1

        # ── Hold the connection open to simulate real work duration ─────────
        # This is what keeps connections occupied and drives up
        # RDS DatabaseConnections under concurrency.
        logger.info(
            json.dumps({
                "event": "holding_connection",
                "request_id": request_id,
                "hold_seconds": HOLD_SECONDS,
                "backend_pid": backend_pid,
            })
        )
        time.sleep(HOLD_SECONDS)

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            json.dumps({
                "event": "request_success",
                "request_id": request_id,
                "duration_ms": duration_ms,
                "backend_pid": backend_pid,
            })
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "status": "ok",
                "scenario": "lambda_db_connection_exhaustion",
                "request_id": request_id,
                "duration_ms": duration_ms,
                "hold_seconds": HOLD_SECONDS,
                "db_version": db_version,
                "backend_pid": backend_pid,
                "note": (
                    "Anti-pattern: new connection per invocation. "
                    "Under concurrency this exhausts RDS max_connections."
                ),
            }),
        }

    except psycopg.OperationalError as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        error_msg = str(exc).strip()

        # Classify the failure for CloudWatch metric filter patterns
        if "too many connections" in error_msg.lower():
            error_type = "too_many_connections"
        elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            error_type = "connection_timeout"
        elif "refused" in error_msg.lower():
            error_type = "connection_refused"
        else:
            error_type = "operational_error"

        logger.error(
            json.dumps({
                "event": "db_connection_failed",
                "error_type": error_type,
                "request_id": request_id,
                "duration_ms": duration_ms,
                "detail": error_msg[:500],
                # Emit the exact string pattern the CloudWatch metric filter looks for
                "scenario": "lambda_db_connection_exhaustion",
                "too many connections": error_type == "too_many_connections",
            })
        )

        # Re-raise so Lambda marks the invocation as an ERROR (drives Alarm 1)
        raise RuntimeError(
            f"DB connection failed [{error_type}]: {error_msg[:200]}"
        ) from exc

    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.error(
            json.dumps({
                "event": "unexpected_error",
                "request_id": request_id,
                "duration_ms": duration_ms,
                "detail": str(exc)[:500],
            })
        )
        raise

    finally:
        # Always close — but under exhaustion conn may be None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
