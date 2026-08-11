"""Prometheus metric definitions.

Label discipline matters more than metric count here: the RCA agent needs to be
able to slice by *endpoint* and *operation*, and Prometheus needs to stay
healthy. So labels are drawn only from bounded sets — route templates
(``/api/users``, never ``/api/users/4213``), fixed operation names, HTTP status
codes, and exception class names. No user ids, emails, request ids, or raw URLs
ever become labels; those live in the structured logs instead.

Process/GC metrics (``process_*``, ``python_*``) come for free from
prometheus-client's default collectors and are exposed on the same endpoint.
"""

from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

# Latency buckets deliberately reach well past 10s: /api/slow-query and
# /api/cpu-stress are meant to land in the tail, and the RCA agent needs to see
# that tail rather than a saturated +Inf bucket.
LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5, 5.0, 10.0, 15.0, 30.0, 60.0,
)

DB_LATENCY_BUCKETS = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5, 5.0, 10.0, 30.0, 60.0,
)

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled.",
    ["method", "endpoint", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "endpoint"],
    buckets=LATENCY_BUCKETS,
)

http_request_errors_total = Counter(
    "http_request_errors_total",
    "HTTP requests that returned a 4xx or 5xx response.",
    ["method", "endpoint", "status"],
)

# Labelled by method only: this gauge has to be incremented *before* routing
# resolves the path, and the raw path would be unbounded.
http_requests_in_progress = Gauge(
    "http_requests_in_progress",
    "HTTP requests currently being processed.",
    ["method"],
)

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

db_queries_total = Counter(
    "db_queries_total",
    "Database operations attempted, by logical operation and outcome.",
    ["operation", "status"],
)

db_query_duration_seconds = Histogram(
    "db_query_duration_seconds",
    "Database operation latency in seconds.",
    ["operation"],
    buckets=DB_LATENCY_BUCKETS,
)

db_errors_total = Counter(
    "db_errors_total",
    "Database operations that failed, by operation and error class.",
    ["operation", "error_type"],
)

db_up = Gauge(
    "db_up",
    "Result of the most recent database connectivity check (1 = reachable).",
)

# --------------------------------------------------------------------------
# Application-specific
# --------------------------------------------------------------------------

app_records_fetched_total = Counter(
    "app_records_fetched_total",
    "Domain records returned to clients, by entity.",
    ["entity"],
)

app_cpu_stress_runs_total = Counter(
    "app_cpu_stress_runs_total",
    "Completed CPU stress runs.",
)

app_cpu_stress_seconds_total = Counter(
    "app_cpu_stress_seconds_total",
    "Cumulative seconds spent burning CPU in the stress endpoint.",
)

app_cpu_stress_active = Gauge(
    "app_cpu_stress_active",
    "CPU stress runs currently in flight.",
)

app_slow_queries_total = Counter(
    "app_slow_queries_total",
    "Deliberately slow database operations executed.",
)

app_simulated_failures_total = Counter(
    "app_simulated_failures_total",
    "Failures raised by the controlled test scenarios, by scenario.",
    ["scenario"],
)

app_db_failure_simulation_active = Gauge(
    "app_db_failure_simulation_active",
    "Whether database-failure simulation is currently enabled (1 = enabled).",
)

app_info = Gauge(
    "app_info",
    "Application build/runtime info, carried as labels (value is always 1).",
    ["app_name", "environment", "version"],
)


def set_app_info(app_name: str, environment: str, version: str) -> None:
    app_info.labels(app_name=app_name, environment=environment, version=version).set(1)


def observe_db_operation(operation: str, duration_seconds: float, status: str) -> None:
    """Record one database operation's outcome and latency."""
    db_query_duration_seconds.labels(operation=operation).observe(duration_seconds)
    db_queries_total.labels(operation=operation, status=status).inc()


def observe_db_error(operation: str, error_type: str) -> None:
    db_errors_total.labels(operation=operation, error_type=error_type).inc()


def registry():
    """The registry scraped by ``GET /metrics`` (prometheus-client default)."""
    return REGISTRY
