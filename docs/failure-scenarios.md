# Failure Scenarios — RCA Test Reference

This is the reference sheet for the four controlled failure scenarios in
`test-rca-app`. Each entry documents the trigger, the expected application
behaviour, the Prometheus signals, the Loki log signals, and the root cause a
correct RCA should land on.

The purpose of these scenarios is to give the Meyiconnect LangGraph RCA agent
(Phase 2, via Grafana MCP → Prometheus/Loki) a set of incidents where the
correct answer is known in advance, so the agent's conclusions can be graded.

## Safety properties

Every scenario is bounded and non-destructive:

| Property | Guarantee |
|---|---|
| No data loss | No scenario deletes, updates, or truncates any table. |
| No unbounded loops | CPU stress checks a monotonic deadline every iteration batch. |
| Hard limits | `CPU_STRESS_MAX_DURATION` and `SLOW_QUERY_MAX_SECONDS` are enforced; out-of-range requests get HTTP 400. |
| No DB damage | The "database failure" is simulated in the application layer. Nothing kills connections or changes the database. |
| No lock contention | `/api/slow-query` uses `pg_sleep`, which is read-only and takes no locks. |
| Bounded concurrency | Concurrent CPU stress runs are capped at the container's core count (HTTP 429 beyond that). |
| Self-terminating | Every scenario ends on its own; none needs a restart to clear (the DB-failure switch is the only stateful one, and it is one call to disarm). |

## Baseline

Set `TRAFFIC_GENERATOR_ENABLED=true` so the app continuously calls its own read
endpoints. Without a baseline there is nothing for an anomaly to stand out
against — no error-rate change to measure, and no "normal" log stream to
contrast with. Let it run 10–15 minutes before triggering a scenario.

Baseline shape: steady `http_requests_total` on `/api/users` and `/api/orders`,
p99 latency in the tens of milliseconds, `db_errors_total` flat, `db_up` at `1`,
and an INFO-only log stream apart from occasional 404s.

---

## Scenario 1 — Database Failure

**Name:** Database connectivity failure
**Class:** External dependency outage
**Severity:** Critical — all data endpoints down

### Trigger

```bash
curl -X POST "http://localhost:8080/api/test/db-failure?enable=true&mode=connection_refused"
```

Modes:

| Mode | Simulates | Latency behaviour |
|---|---|---|
| `connection_refused` | Database process down / security group blocking the port | Fails fast |
| `connection_timeout` | Network blackhole, SG drop, overloaded DB not accepting connections | Burns `DB_FAILURE_DELAY_SECONDS` first, then fails |
| `query_error` | Server closed the connection mid-statement | Fails fast |

Alternatively start the container with `SIMULATE_DB_FAILURE=true`.

Disarm:

```bash
curl -X POST "http://localhost:8080/api/test/db-failure?enable=false"
```

### Expected application behaviour

- `GET /api/users`, `GET /api/orders`, `GET /api/db-check`, `GET /ready` → **HTTP 503**
- Response body names the failure class only — never host, user, password, or DSN
- `GET /health` → **still 200 `{"status":"healthy"}`** (default `HEALTH_CHECK_DB=false`)

That last point is deliberate and is the key discriminator for this scenario:
the application process is healthy, its dependency is not. The container is not
restarting, and it is not out of CPU.

### Expected Prometheus metrics

| Metric | Expected change |
|---|---|
| `db_errors_total{operation="select_users",error_type="connection_refused"}` | **Rising** — the primary signal |
| `db_queries_total{operation="select_users",status="error"}` | Rising |
| `db_queries_total{status="success"}` | Flat (stops increasing) |
| `db_up` | **0** |
| `http_requests_total{endpoint="/api/users",status="503"}` | Rising |
| `http_request_errors_total{endpoint="/api/users",status="503"}` | Rising |
| `app_db_failure_simulation_active` | `1` (confirms it is a drill, not a real outage) |
| `http_request_duration_seconds` | Flat for `connection_refused`; **elevated** for `connection_timeout` |
| `process_cpu_seconds_total` | Normal or lower — CPU is not the problem |

Useful queries:

```promql
sum(rate(db_errors_total[5m])) by (operation, error_type)
sum(rate(http_requests_total{status="503"}[5m])) by (endpoint)
db_up
```

### Expected Loki logs

```logql
{container="test-rca-app"} | json | level="ERROR" | operation="select_users"
```

```json
{
  "timestamp": "2026-08-11T09:14:02.417+00:00",
  "level": "ERROR",
  "service": "test-rca-app",
  "environment": "test",
  "operation": "select_users",
  "status": "error",
  "error_type": "connection_refused",
  "duration_ms": 1.84,
  "db_target": "db.example.internal:5432",
  "message": "Database operation failed",
  "detail": "could not connect to server: Connection refused..."
}
```

Correlated lines: `message="Request failed with server error"` with
`status=503` and `endpoint="/api/users"`, plus a WARNING
`operation="db_failure_simulation"` marking the moment the switch flipped.

Absent: no CPU stress lines, no application stack traces.

### Expected RCA

> **Root cause:** The application cannot reach its PostgreSQL database. Every
> database-backed endpoint fails with connection errors to
> `<db_target>`, while the application process itself stays healthy
> (`/health` 200, CPU and memory normal). This is an external dependency
> failure, not an application defect.
>
> **Evidence:** `db_errors_total{error_type="connection_refused"}` rising in
> lockstep with `http_requests_total{status="503"}`; `db_up == 0`; ERROR logs
> with `operation="select_users"` and connection-refused detail; no stack traces
> from application code.
>
> **Recommended remediation:** Verify the database instance is running and
> accepting connections; check the security group / network path to the
> database port; check connection limits and credentials. In this test
> environment, confirm `app_db_failure_simulation_active == 1` and disarm the
> switch.

### Discriminators

- vs. **application error**: `db_errors_total` moves, and there is *no*
  application stack trace.
- vs. **database latency**: requests fail rather than succeed slowly;
  `db_queries_total{status="error"}` rises instead of `db_query_duration_seconds`.

---

## Scenario 2 — CPU Stress

**Name:** CPU saturation
**Class:** Resource exhaustion
**Severity:** Degradation — requests slow, nothing fails outright

### Trigger

```bash
curl "http://localhost:8080/api/cpu-stress?duration=30"
```

Bounded by `CPU_STRESS_MAX_DURATION` (default 120s). `duration<=0` or above the
cap → HTTP 400. Concurrent runs beyond the core count → HTTP 429.

To make the effect obvious, drive several at once and keep the baseline running:

```bash
for i in 1 2 3; do curl -s "http://localhost:8080/api/cpu-stress?duration=60" & done; wait
```

### Expected application behaviour

- HTTP 200 with `{"scenario":"cpu_stress","actual_seconds":~duration}`
- The request itself is long-running by design
- Other endpoints stay functional but get slower under contention
- Load stops on its own at the deadline

### Expected Prometheus metrics

| Metric | Expected change |
|---|---|
| `rate(process_cpu_seconds_total[1m])` | **Sharp rise**, approaching the CPU limit — the primary signal |
| `app_cpu_stress_active` | `> 0` during the run |
| `app_cpu_stress_runs_total` | +1 per completed run |
| `app_cpu_stress_seconds_total` | Rising by roughly the requested duration |
| `http_request_duration_seconds{endpoint="/api/cpu-stress"}` | Lands in the 15s/30s/60s buckets |
| `http_request_duration_seconds{endpoint="/api/users"}` | **Elevated** — collateral latency |
| `db_query_duration_seconds` | Roughly normal — the database is fine |
| `db_errors_total` | **Flat** |
| `http_requests_total{status="5.."}` | Flat |

```promql
rate(process_cpu_seconds_total[1m])
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{endpoint="/api/users"}[5m])) by (le))
app_cpu_stress_active
```

### Expected Loki logs

```logql
{container="test-rca-app"} | json | scenario="cpu_stress"
```

```json
{"timestamp":"...","level":"WARNING","operation":"cpu_stress","scenario":"cpu_stress","requested_duration_s":30,"message":"CPU stress started"}
{"timestamp":"...","level":"WARNING","operation":"cpu_stress","scenario":"cpu_stress","status":"success","requested_duration_s":30,"actual_duration_s":30.004,"duration_ms":30004.21,"message":"CPU stress completed"}
```

Correlated: `message="Slow request completed"` WARNINGs on *other* endpoints
during the window. Absent: no ERROR logs, no database errors.

### Expected RCA

> **Root cause:** CPU saturation inside the application container. Process CPU
> ran at its limit for the duration of the incident, inflating latency across
> all endpoints. The database was healthy throughout (no DB errors, normal query
> duration), and no requests failed — this is resource exhaustion, not a fault.
>
> **Evidence:** `rate(process_cpu_seconds_total[1m])` at ceiling; latency
> elevated on unrelated endpoints; `app_cpu_stress_active > 0`; "CPU stress
> started"/"completed" WARNING logs bracketing the window exactly;
> `db_errors_total` flat.
>
> **Recommended remediation:** Identify the CPU-intensive code path (here, the
> `/api/cpu-stress` endpoint), raise the container CPU limit or scale
> horizontally, and add an alert on sustained CPU above ~80%.

### Discriminators

- Latency rises on **all** endpoints, not just database-backed ones.
- No errors at all — distinguishes this from both failure scenarios.
- `db_query_duration_seconds` stays normal — distinguishes it from Scenario 4.

---

## Scenario 3 — Application Exception

**Name:** Unhandled application exception
**Class:** Application defect
**Severity:** High — a specific endpoint is broken

### Trigger

```bash
curl -i http://localhost:8080/api/error
curl -i "http://localhost:8080/api/error?kind=zero_division"
```

`kind` ∈ `runtime` (default), `value`, `key`, `zero_division`, `type`. An unknown
kind → HTTP 400 (a validation response, not a scenario).

### Expected application behaviour

- **HTTP 500** with `{"error":"internal_server_error","error_type":"...","request_id":"..."}`
- Only `/api/error` is affected; every other endpoint keeps working
- The response never leaks configuration or connection details

### Expected Prometheus metrics

| Metric | Expected change |
|---|---|
| `http_requests_total{endpoint="/api/error",status="500"}` | **Rising** — the primary signal |
| `http_request_errors_total{endpoint="/api/error",status="500"}` | Rising |
| `app_simulated_failures_total{scenario="application_error"}` | Rising |
| `db_errors_total` | **Flat** — the database is not involved |
| `db_up` | `1` |
| `http_request_duration_seconds{endpoint="/api/error"}` | **Fast** — it fails immediately |
| `process_cpu_seconds_total` | Normal |

```promql
sum(rate(http_requests_total{status="500"}[5m])) by (endpoint)
sum(rate(db_errors_total[5m]))   # expected: 0
```

### Expected Loki logs

```logql
{container="test-rca-app"} | json | level="ERROR" | endpoint="/api/error"
```

```json
{
  "timestamp": "2026-08-11T09:31:44.902+00:00",
  "level": "ERROR",
  "service": "test-rca-app",
  "environment": "test",
  "request_id": "9f2c41ab77e0d155",
  "endpoint": "/api/error",
  "method": "GET",
  "status": 500,
  "duration_ms": 3.11,
  "operation": "http_request",
  "error_type": "SimulatedApplicationError",
  "message": "Unhandled exception while processing request",
  "error_message": "Simulated application failure while processing order payload",
  "stack_trace": "Traceback (most recent call last):\n  File \"/app/app/routes/test_failures.py\", line 177, in application_error\n    _raise_kind(kind)\n..."
}
```

The `stack_trace` field names the failing file, line, and function — that is
what makes this scenario attributable to code rather than infrastructure.

Absent: no `operation="database_query"` errors, no CPU stress lines.

### Expected RCA

> **Root cause:** An application defect in the `/api/error` handler. The
> endpoint raises an unhandled `SimulatedApplicationError`, returning HTTP 500
> immediately. Failures are confined to this one route; the database is healthy
> (`db_up == 1`, no DB errors) and resources are normal. The stack trace
> attributes the fault to `app/routes/test_failures.py`.
>
> **Evidence:** `http_requests_total{endpoint="/api/error",status="500"}` rising
> while all other endpoints serve 200s; `db_errors_total` flat; ERROR logs with
> `error_type` and a stack trace pointing at application code; low
> `duration_ms` (a code path failing fast, not a timeout).
>
> **Recommended remediation:** Fix the exception at the location named in the
> stack trace and add error handling around it. No infrastructure change is
> warranted.

### Discriminators

- **Errors are scoped to one endpoint** — dependency failures hit every
  database-backed route.
- A **stack trace in application code** is present; DB failures produce
  driver-level connection errors instead.
- **Fast failures** — timeouts and latency incidents are slow.

---

## Scenario 4 — Database Latency

**Name:** Database query latency
**Class:** Dependency performance degradation
**Severity:** Degradation — requests succeed, slowly

### Trigger

```bash
curl "http://localhost:8080/api/slow-query?seconds=10"
```

Defaults to `SLOW_QUERY_DEFAULT_SECONDS`; capped by `SLOW_QUERY_MAX_SECONDS`
(default 30). Out-of-range → HTTP 400.

Sustained latency for a realistic incident window:

```bash
for i in $(seq 1 12); do curl -s "http://localhost:8080/api/slow-query?seconds=10" >/dev/null; done
```

### Expected application behaviour

- **HTTP 200**, returning after the requested delay
- Implemented as `SELECT pg_sleep(n)` — read-only, takes no locks, self-terminating
- Holds one pooled connection for the duration; with enough concurrency, pool
  waits appear as `pool_timeout` errors (also a realistic signal)

### Expected Prometheus metrics

| Metric | Expected change |
|---|---|
| `db_query_duration_seconds{operation="slow_query"}` | **Sharp rise** in `_sum`, tail buckets filling — the primary signal |
| `histogram_quantile(0.95, ...db_query_duration_seconds_bucket...)` | Seconds instead of milliseconds |
| `db_queries_total{operation="slow_query",status="success"}` | Rising — succeeding, not failing |
| `app_slow_queries_total` | Rising |
| `http_request_duration_seconds{endpoint="/api/slow-query"}` | Rising in step with DB duration |
| `db_errors_total` | **Flat** |
| `http_requests_total{status="5.."}` | Flat |
| `process_cpu_seconds_total` | **Normal or low** — waiting on I/O, not burning CPU |

```promql
histogram_quantile(0.95, sum(rate(db_query_duration_seconds_bucket[5m])) by (le, operation))
sum(rate(db_queries_total{status="success"}[5m])) by (operation)
```

The pairing of high DB duration with normal CPU is the signature of this
scenario.

### Expected Loki logs

```logql
{container="test-rca-app"} | json | operation="slow_query"
```

```json
{
  "timestamp": "2026-08-11T09:47:12.338+00:00",
  "level": "WARNING",
  "service": "test-rca-app",
  "operation": "slow_query",
  "scenario": "database_latency",
  "status": "success",
  "requested_duration_s": 10.0,
  "actual_duration_s": 10.021,
  "duration_ms": 10021.4,
  "db_target": "db.example.internal:5432",
  "message": "Slow database operation completed"
}
```

`tracked_db_operation` also emits `message="Slow database operation"` for *any*
DB call over its threshold, so genuine latency is logged even when it was not
triggered on purpose. Correlated: `message="Slow request completed"` WARNINGs
with a matching `duration_ms`.

Absent: no ERROR level lines, no stack traces.

### Expected RCA

> **Root cause:** Database query latency. Query duration for the `slow_query`
> operation rose from milliseconds to ~10s, and HTTP latency tracked it almost
> exactly. Requests still succeed (no errors, `db_up == 1`) and application CPU
> is normal, so the application is waiting on the database rather than doing
> excess work. This is dependency performance degradation, not an outage or an
> application defect.
>
> **Evidence:** `db_query_duration_seconds` p95 in seconds; HTTP latency rising
> in step; `db_queries_total{status="success"}` still climbing while
> `db_errors_total` stays flat; `process_cpu_seconds_total` normal; WARNING
> "Slow database operation" logs with `duration_ms ≈ 10000`.
>
> **Recommended remediation:** Inspect the slow statement (`pg_stat_statements`),
> check for missing indexes, lock waits, and database-side CPU/IO pressure, and
> review connection-pool sizing since long queries hold connections. Add an
> alert on DB p95 latency.

### Discriminators

- **Requests succeed** — separates this from Scenarios 1 and 3.
- **DB duration rises while CPU stays normal** — separates it from Scenario 2,
  where CPU is the mover and DB duration is not.
- Latency is concentrated in **database operations**, not spread evenly across
  all endpoints.

---

## Summary matrix

The four scenarios are separable on metrics alone — this is the table an RCA
agent should effectively reconstruct:

| Signal | 1. DB failure | 2. CPU stress | 3. App exception | 4. DB latency |
|---|---|---|---|---|
| HTTP status | 503 | 200 | 500 | 200 |
| `db_errors_total` | **Rising** | Flat | Flat | Flat |
| `db_up` | **0** | 1 | 1 | 1 |
| `db_query_duration_seconds` | n/a (failing) | Normal | Not involved | **Rising** |
| `process_cpu_seconds_total` | Normal | **At ceiling** | Normal | Normal |
| HTTP latency | Fast fail (or slow on timeout mode) | **Elevated everywhere** | Fast fail | **Elevated on DB routes** |
| Log level | ERROR | WARNING | ERROR | WARNING |
| Stack trace in app code | No | No | **Yes** | No |
| Scope | All DB endpoints | All endpoints | One endpoint | DB endpoints |
| `/health` | 200 | 200 | 200 | 200 |

## Combining scenarios

For a harder test, overlap two — e.g. CPU stress plus database latency — and
check whether the agent identifies both contributors instead of stopping at the
first. Reset between runs by disarming the DB-failure switch and letting the
baseline settle for ~10 minutes so histogram rates return to normal.
