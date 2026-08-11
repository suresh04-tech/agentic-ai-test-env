# agentic-ai-test-env

A controlled observability / RCA test environment for the **Meyiconnect Agentic
AI** project (GENAIPR-002, Phase 1).

This single repository contains **both halves** of the environment:

- **The application under test** (`test-rca-app`) — FastAPI over PostgreSQL,
  behind Nginx. It looks and behaves like a small customer web application, but
  its real job is to emit **Prometheus metrics** and **structured JSON logs**
  that tell a coherent story, and to fail **on demand, safely, in four distinct
  ways**.
- **The observability stack** — Prometheus, Loki, Grafana Alloy, Grafana, and
  node-exporter, all in the same `docker-compose.yml`, so one
  `docker compose up -d --build` brings up everything.

Read it in two parts: **Part A** (sections 1–24) documents the application;
**Part B** (sections 25–46) documents the observability stack.

---

## Table of contents

**Part A — The application**

1. [What this application is](#1-what-this-application-is)
2. [Why it exists](#2-why-it-exists)
3. [Architecture](#3-architecture)
4. [Project structure](#4-project-structure)
5. [Local development](#5-local-development)
6. [Environment variables](#6-environment-variables)
7. [PostgreSQL configuration](#7-postgresql-configuration)
8. [Docker build](#8-docker-build)
9. [Docker Compose startup](#9-docker-compose-startup)
10. [API endpoints](#10-api-endpoints)
11. [Prometheus metrics](#11-prometheus-metrics)
12. [Logging format](#12-logging-format)
13. [Failure scenarios](#13-failure-scenarios)
14. [Testing database failure](#14-testing-database-failure)
15. [Testing CPU stress](#15-testing-cpu-stress)
16. [Testing application error](#16-testing-application-error)
17. [Testing database latency](#17-testing-database-latency)
18. [Expected Prometheus signals](#18-expected-prometheus-signals)
19. [Expected Loki log signals](#19-expected-loki-log-signals)
20. [EC2 deployment](#20-ec2-deployment)
21. [Running the tests](#21-running-the-tests)
22. [Troubleshooting](#22-troubleshooting)
23. [Security notes](#23-security-notes)
24. [Scope: what this repository is not](#24-scope-what-this-repository-is-not)

**Part B — The observability stack**

25. [Existing application architecture](#25-existing-application-architecture)
26. [New observability architecture](#26-new-observability-architecture)
27. [Why Prometheus](#27-why-prometheus)
28. [Why Loki](#28-why-loki)
29. [Why Grafana Alloy](#29-why-grafana-alloy)
30. [Why Grafana](#30-why-grafana)
31. [Why node_exporter](#31-why-node_exporter)
32. [Port usage](#32-port-usage)
33. [Docker network](#33-docker-network)
34. [Starting the complete stack](#34-starting-the-complete-stack)
35. [Stopping the stack](#35-stopping-the-stack)
36. [Checking containers](#36-checking-containers)
37. [Accessing Grafana](#37-accessing-grafana)
38. [Grafana login](#38-grafana-login)
39. [Prometheus verification](#39-prometheus-verification)
40. [Loki verification](#40-loki-verification)
41. [Alloy verification](#41-alloy-verification)
42. [Application metrics verification](#42-application-metrics-verification)
43. [Application log verification](#43-application-log-verification)
44. [RCA failure scenario testing](#44-rca-failure-scenario-testing)
45. [Observability troubleshooting](#45-observability-troubleshooting)
46. [Future Grafana MCP integration](#46-future-grafana-mcp-integration)

---

## 1. What this application is

A single FastAPI service that:

- serves a few read endpoints backed by an **external PostgreSQL** database
- exposes **Prometheus metrics** on `/metrics`
- writes **structured JSON logs to stdout** (never to files)
- can generate its own **baseline HTTP traffic**
- provides **four bounded, non-destructive failure scenarios** for RCA testing
- runs in Docker as a non-root user, configured entirely by environment variables

The code structure is production-quality; the functionality is intentionally
small. It is a test instrument, not a product.

## 2. Why it exists

Meyiconnect's RCA pipeline is:

```
AWS alarm / monitoring issue
  → Incident
  → Deterministic enrichment / pre-triage
  → LangGraph RCA Agent
  → Investigation tools
  → Evidence correlation
  → Root Cause Analysis
  → Remediation recommendation
```

The agent already has AWS investigation tools (EC2, CloudWatch metrics and logs,
CloudTrail, ALB and target health, network, security groups, infrastructure
changes). GENAIPR-002 adds **external observability via MCP**, starting with:

```
Grafana MCP → Prometheus → metrics
Grafana MCP → Loki       → logs
```

Some customers run Grafana + Prometheus + Loki instead of CloudWatch for
application telemetry. To develop and grade that capability we need a target
application where **the correct root cause is known in advance**. That is this
repository.

Phase 1 (this repo) prepares the environment — application *and* observability
stack. Phase 2 wires Grafana MCP into the LangGraph agent; no MCP or agent code
lives here.

## 3. Architecture

Phase-1 environment on a single EC2 instance. Everything below runs from this
repository's single `docker-compose.yml`:

```
                             AWS EC2
                                |
                +---------------+----------------+
                |                                |
          Application                       Observability
                |                                |
        +-------+-------+         +----------+---+-------+-----------+
        |               |         |          |           |           |
      nginx:80     test-rca-app  Prometheus Alloy       Loki    node-exporter
        |               |         ^          |           ^           |
        +---> app:8080 -+         |          |           |           |
                        |         |          |           |           |
                   /metrics ------+          |           |           |
                        |                    |           |           |
                   stdout logs --> json-file +---------> +           |
                                                          |          |
                                              Prometheus <-----------+
                                                          |
                                                       Grafana :3000
                                                          |
                                                   Grafana MCP
                                                          |
                                              Phase 2: LangGraph RCA
```

Data flow out of the application:

| Path | Consumer |
|---|---|
| `GET /metrics` (HTTP pull) | Prometheus scrape, via `test-rca-app:8080` |
| stdout → Docker `json-file` log driver | Grafana Alloy → Loki |

The app connects **out** to an external PostgreSQL database. Full detail on the
observability half is in [Part B](#26-new-observability-architecture).

## 4. Project structure

```
agentic-ai-test-env/
├── app/                            # the application under test
│   ├── __init__.py
│   ├── main.py                  # app factory, lifespan, global error handler
│   ├── config.py                # env-driven settings, credential redaction
│   ├── database.py              # engine, instrumented ops, failure simulation
│   ├── models.py                # users, orders
│   ├── metrics.py               # Prometheus metric definitions
│   ├── logging_config.py        # JSON formatter -> stdout
│   ├── middleware.py            # per-request metrics, access log, request id
│   ├── traffic.py               # optional baseline traffic generator
│   └── routes/
│       ├── __init__.py
│       ├── _common.py           # DB error -> HTTP mapping
│       ├── health.py            # /health, /ready, /metrics, /api/db-check
│       ├── users.py             # /api/users
│       ├── orders.py            # /api/orders
│       └── test_failures.py     # the four scenarios
├── tests/
│   ├── conftest.py              # forces in-memory SQLite; never a real DB
│   ├── unit/                    # 58 tests, no external dependencies
│   └── integration/             # opt-in, real PostgreSQL
├── scripts/
│   ├── generate_traffic.sh      # external baseline load
│   └── smoke_test.sh            # verify every endpoint + scenario
├── docs/
│   └── failure-scenarios.md     # RCA reference for all four scenarios
│
├── prometheus/
│   └── prometheus.yml           # scrape config (app + node-exporter + self)
├── loki/
│   └── loki-config.yml          # single-node Loki: filesystem + TSDB
├── alloy/
│   └── config.alloy             # Docker log discovery -> Loki
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       │   └── datasources.yml  # Prometheus + Loki, auto-provisioned
│       └── dashboards/
│           ├── dashboards.yml   # dashboard provider
│           └── test-rca-app-overview.json
│
├── nginx.conf                   # reverse proxy :80 -> app:8080
├── Dockerfile
├── docker-compose.yml           # ONE file: app + nginx + full observability
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
```

## 5. Local development

Requires Python 3.12+ and a reachable PostgreSQL database.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env               # then edit .env with your database details
```

Load `.env` and start the server with reload:

```bash
set -a && . ./.env && set +a && uvicorn app.main:app --reload --port 8080
```

Then:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/docs     # interactive OpenAPI UI
```

Unit tests need no database at all (they use in-memory SQLite):

```bash
pytest
```

## 6. Environment variables

All configuration is environment-based. There are **no defaults for anything
sensitive**; the app refuses to start without database configuration.

### Application

| Variable | Default | Purpose |
|---|---|---|
| `APP_NAME` | `test-rca-app` | `service` field in logs, `app_info` label |
| `APP_ENV` | `test` | `environment` field in logs, `app_info` label |
| `APP_VERSION` | `1.0.0` | `version` field in logs, `app_info` label |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PORT` | `8080` | Listen port for local runs; also the traffic generator's self-target |
| `APP_PORT` | `8080` | Host port published by docker compose (container is always 8080) |

### Database (required)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | Full connection string. Takes precedence. |
| `DATABASE_HOST` | — | Used only if `DATABASE_URL` is unset |
| `DATABASE_PORT` | `5432` | " |
| `DATABASE_NAME` | — | " |
| `DATABASE_USER` | — | " |
| `DATABASE_PASSWORD` | — | " |
| `DB_POOL_SIZE` | `5` | SQLAlchemy pool size |
| `DB_MAX_OVERFLOW` | `5` | Extra connections above the pool |
| `DB_TIMEOUT` | `10` | Connect timeout and pool checkout timeout (seconds) |
| `DB_STATEMENT_TIMEOUT_MS` | `60000` | PostgreSQL `statement_timeout`. Must exceed `SLOW_QUERY_MAX_SECONDS`. |
| `DB_AUTO_INIT` | `true` | Create tables at startup if missing |
| `DB_SEED` | `true` | Insert seed rows when `users` is empty (idempotent) |
| `DB_SEED_USERS` | `25` | Seed user count |

### Health

| Variable | Default | Purpose |
|---|---|---|
| `HEALTH_CHECK_DB` | `false` | Include the database in `/health`. Keep `false` so a DB outage shows as failing endpoints rather than a dead container. |

### Failure scenario bounds

| Variable | Default | Purpose |
|---|---|---|
| `CPU_STRESS_MAX_DURATION` | `120` | Hard cap on `/api/cpu-stress?duration=` (seconds) |
| `SLOW_QUERY_MAX_SECONDS` | `30` | Hard cap on `/api/slow-query?seconds=` |
| `SLOW_QUERY_DEFAULT_SECONDS` | `5` | Default when `seconds` is omitted (clamped to the cap) |
| `SIMULATE_DB_FAILURE` | `false` | Start with the DB-failure simulation already armed |
| `DB_FAILURE_MODE` | `connection_refused` | `connection_refused` \| `connection_timeout` \| `query_error` |
| `DB_FAILURE_DELAY_SECONDS` | `2` | Latency burned before failing in `connection_timeout` mode |
| `ENABLE_TEST_CONTROLS` | `true` | Enables `POST /api/test/db-failure`. Set `false` to freeze the switch. |

### Baseline traffic generator

| Variable | Default | Purpose |
|---|---|---|
| `TRAFFIC_GENERATOR_ENABLED` | `false` | Call own read endpoints to produce a metric/log baseline |
| `TRAFFIC_GENERATOR_RPS` | `2` | Approximate requests per second |
| `TRAFFIC_GENERATOR_BASE_URL` | `http://127.0.0.1:$PORT` | Self-target |

Recommended for the Phase-1 environment: `TRAFFIC_GENERATOR_ENABLED=true`. An
RCA agent cannot detect an anomaly without a baseline to compare against.

## 7. PostgreSQL configuration

The database is **external** — this repo never runs one. Any reachable
PostgreSQL works (RDS, Render, a container you manage separately).

Either set a full URL:

```bash
DATABASE_URL=postgresql://username:password@hostname:5432/database
```

or the discrete parts (used only when `DATABASE_URL` is unset), which are
percent-encoded for you, so passwords with `@`, `/`, or spaces are safe:

```bash
DATABASE_HOST=hostname
DATABASE_PORT=5432
DATABASE_NAME=database
DATABASE_USER=username
DATABASE_PASSWORD=password
```

`postgresql://` and `postgres://` are rewritten to `postgresql+psycopg://`
internally (psycopg 3), so either form works.

**TLS.** Managed databases usually require it — append `?sslmode=require`:

```bash
DATABASE_URL=postgresql://username:password@hostname:5432/database?sslmode=require
```

**Schema.** Created at startup when `DB_AUTO_INIT=true`:

```sql
users   (id, name, email UNIQUE, created_at)
orders  (id, user_id -> users.id, amount NUMERIC(10,2), status, created_at)
```

`create_all` is idempotent; seeding only runs when `users` is empty. There is no
migration framework — deliberate, for a throwaway test app. The database user
needs `CREATE TABLE` on its schema, or set `DB_AUTO_INIT=false` and create the
tables yourself.

**Startup with an unreachable database is not fatal.** The app starts, logs the
failure, and serves 503s from data endpoints until the database returns. That is
the behaviour we want for RCA testing.

## 8. Docker build

```bash
docker build -t test-rca-app:latest .
```

The image runs Uvicorn as non-root user `appuser` (uid 10001), exposes 8080, and
has a `curl`-based healthcheck against `/health`.

Run it directly, passing configuration at runtime (nothing is baked in):

```bash
docker run -d --name test-rca-app -p 8080:8080 --env-file .env test-rca-app:latest
```

A single Uvicorn worker is intentional: prometheus-client counters live in
process memory, and multiple workers would each expose a partial view.

## 9. Docker Compose startup

```bash
cp .env.example .env      # edit .env with real values first
docker compose build
docker compose up -d
docker compose logs -f app
```

Verify:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/metrics
```

Stop:

```bash
docker compose down
```

This one compose file runs the **whole environment**: Nginx, the application,
and the observability stack. The application container is not published to the
host at all — it is reached through Nginx on port 80 — and is capped at 1.5 CPU
/ 512 MB, so CPU saturation is visible but bounded. Its `json-file` log driver
carries `service`, `environment`, and `component` labels, which Alloy promotes
into Loki labels.

**Prometheus scrapes the app over the Docker network** at
`http://test-rca-app:8080/metrics` — no host port and no EC2 public IP
involved. Compose creates the shared `observability` network automatically;
there is no `docker network create` step. See
[Part B](#26-new-observability-architecture) for the full stack.

## 10. API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Service banner and endpoint list |
| GET | `/health` | Liveness. `{"status":"healthy"}` |
| GET | `/ready` | Readiness — always checks the database |
| GET | `/metrics` | Prometheus scrape endpoint |
| GET | `/api/users` | List users (`?limit=1..500`) |
| GET | `/api/orders` | List orders (`?limit=1..500`, `?status=`) |
| GET | `/api/db-check` | Database connectivity test |
| GET | `/api/slow-query` | **Scenario 4** — bounded database latency |
| GET | `/api/error` | **Scenario 3** — controlled application exception |
| GET | `/api/cpu-stress` | **Scenario 2** — bounded CPU load |
| GET | `/api/test/db-failure` | **Scenario 1** — read the failure switch |
| POST | `/api/test/db-failure` | **Scenario 1** — arm/disarm the failure switch |
| GET | `/docs` | OpenAPI UI |

Every endpoint, as curl:

```bash
curl http://localhost:8080/
curl http://localhost:8080/health
curl http://localhost:8080/ready
curl http://localhost:8080/metrics
curl http://localhost:8080/api/users
curl "http://localhost:8080/api/users?limit=5"
curl http://localhost:8080/api/orders
curl "http://localhost:8080/api/orders?status=paid&limit=10"
curl http://localhost:8080/api/db-check
curl http://localhost:8080/api/slow-query
curl "http://localhost:8080/api/slow-query?seconds=10"
curl http://localhost:8080/api/error
curl "http://localhost:8080/api/error?kind=zero_division"
curl "http://localhost:8080/api/cpu-stress?duration=30"
curl http://localhost:8080/api/test/db-failure
curl -X POST "http://localhost:8080/api/test/db-failure?enable=true&mode=connection_refused"
curl -X POST "http://localhost:8080/api/test/db-failure?enable=false"
```

Or run all of it at once, including every scenario:

```bash
./scripts/smoke_test.sh http://localhost:8080
```

Sample responses:

```bash
$ curl -s http://localhost:8080/health
{"status":"healthy"}

$ curl -s "http://localhost:8080/api/users?limit=2"
{"count":2,"users":[{"id":1,"name":"Nikhil Menon","email":"nikhil.menon0@example.test","created_at":"2026-08-11T09:02:11.418293+00:00"}, ...]}

$ curl -s "http://localhost:8080/api/orders?limit=1"
{"count":1,"orders":[{"id":48,"user_id":24,"amount":612.44,"status":"paid","created_at":"2026-08-11T09:02:11.492817+00:00"}]}

$ curl -s http://localhost:8080/api/db-check
{"database":"reachable","target":"db.example.internal:5432","failure_simulation_enabled":false}
```

## 11. Prometheus metrics

`GET /metrics` returns the standard Prometheus text format.

### HTTP

| Metric | Type | Labels |
|---|---|---|
| `http_requests_total` | Counter | `method`, `endpoint`, `status` |
| `http_request_duration_seconds` | Histogram | `method`, `endpoint` |
| `http_request_errors_total` | Counter | `method`, `endpoint`, `status` |
| `http_requests_in_progress` | Gauge | `method` |

Buckets run to 60s so `/api/slow-query` and `/api/cpu-stress` land in the tail
rather than saturating `+Inf`.

### Database

| Metric | Type | Labels |
|---|---|---|
| `db_queries_total` | Counter | `operation`, `status` |
| `db_query_duration_seconds` | Histogram | `operation` |
| `db_errors_total` | Counter | `operation`, `error_type` |
| `db_up` | Gauge | — |

`operation` ∈ `connectivity_check`, `select_users`, `select_orders`,
`slow_query`, `schema_init`, `seed_check`, `seed_insert`.
`error_type` ∈ `connection_refused`, `connection_timeout`, `query_error`,
`pool_timeout`, `integrity_error`, `authentication_error`, `connection_limit`,
`operational_error`, `dbapi_error`.

### Application

| Metric | Type | Labels |
|---|---|---|
| `app_records_fetched_total` | Counter | `entity` (`users`, `orders`) |
| `app_cpu_stress_runs_total` | Counter | — |
| `app_cpu_stress_seconds_total` | Counter | — |
| `app_cpu_stress_active` | Gauge | — |
| `app_slow_queries_total` | Counter | — |
| `app_simulated_failures_total` | Counter | `scenario` |
| `app_db_failure_simulation_active` | Gauge | — |
| `app_info` | Gauge | `app_name`, `environment`, `version` |

### Process metrics

prometheus-client's default collectors add `process_cpu_seconds_total`,
`process_resident_memory_bytes`, `process_open_fds`, and `python_gc_*` on the
same endpoint. `process_cpu_seconds_total` is the primary signal for Scenario 2.

### Cardinality discipline

`endpoint` is always the matched **route template** (`/api/users`), never the raw
path; unmatched paths collapse to `endpoint="unmatched"`. No user id, email,
request id, or raw URL is ever a label — those live in the logs. `db_errors_total`
uses a fixed, classified `error_type` rather than raw driver text. Two unit tests
enforce this.

## 12. Logging format

One JSON object per line, to **stdout only** — nothing is written to files, so
the Docker `json-file` driver captures everything and Alloy ships it to Loki.
Uvicorn's own loggers are re-formatted as JSON too.

Always present: `timestamp` (ISO-8601 UTC, ms), `level`, `service`,
`environment`, `version`, `logger`, `message`, and `request_id` when in a
request.

Context fields, when relevant: `endpoint`, `method`, `status`, `duration_ms`,
`operation`, `error_type`, `scenario`, `rows`, `db_target`,
`requested_duration_s`, `actual_duration_s`, `failure_mode`, `client_ip`,
`user_agent`, `detail`. Errors add `error_message` and `stack_trace`.

A normal request:

```json
{"timestamp":"2026-08-11T09:02:14.881+00:00","level":"INFO","service":"test-rca-app","environment":"test","version":"1.0.0","logger":"app.middleware","message":"Request completed","request_id":"3f9a1c77b2e04d16","endpoint":"/api/users","method":"GET","status":200,"duration_ms":11.42,"operation":"http_request","client_ip":"172.18.0.1","user_agent":"curl/8.5.0"}
```

A database failure:

```json
{"timestamp":"2026-08-11T09:14:02.417+00:00","level":"ERROR","service":"test-rca-app","environment":"test","version":"1.0.0","logger":"app.database","message":"Database operation failed","request_id":"a71b3e5590c2f844","operation":"select_users","status":"error","error_type":"connection_refused","duration_ms":1.84,"db_target":"db.example.internal:5432","detail":"could not connect to server: Connection refused..."}
```

**Never logged:** passwords, tokens, API keys, or the full `DATABASE_URL`. Only
`host:port` appears, as `db_target`. The formatter additionally redacts a
known set of sensitive keys to `***` and drops any unrecognised extra field, so
a careless `extra={"password": ...}` cannot leak. Enforced by unit tests.

Suggested LogQL:

```logql
{container="test-rca-app"} | json | level="ERROR"
{container="test-rca-app"} | json | operation="select_users" | error_type="connection_refused"
{container="test-rca-app"} | json | duration_ms > 1000
{container="test-rca-app"} | json | scenario="cpu_stress"
sum(rate({container="test-rca-app"} | json | level="ERROR" [5m]))
```

## 13. Failure scenarios

| # | Scenario | Trigger | Result | Bound |
|---|---|---|---|---|
| 1 | Database failure | `POST /api/test/db-failure?enable=true` | 503 from data endpoints | Reversible switch; DB untouched |
| 2 | CPU stress | `GET /api/cpu-stress?duration=30` | High CPU, elevated latency | `CPU_STRESS_MAX_DURATION`, core-count concurrency cap |
| 3 | Application error | `GET /api/error` | 500 + stack trace | Stateless |
| 4 | Database latency | `GET /api/slow-query?seconds=10` | Slow 200s | `SLOW_QUERY_MAX_SECONDS` |

Nothing here deletes data, drops tables, kills connections, or loops without a
deadline. Full RCA reference — including per-scenario expected metrics, logs,
and the correct root cause — is in
[`docs/failure-scenarios.md`](docs/failure-scenarios.md).

## 14. Testing database failure

The switch is **application-level**: it makes database calls raise synthetic
connection errors. The real database is never modified, so this is safe against
a shared or managed instance.

```bash
# Arm it
curl -X POST "http://localhost:8080/api/test/db-failure?enable=true&mode=connection_refused"

# Data endpoints now fail with 503
curl -i http://localhost:8080/api/users
curl -i http://localhost:8080/api/orders
curl -i http://localhost:8080/api/db-check

# Liveness still passes — the app is up, its dependency is not
curl http://localhost:8080/health      # {"status":"healthy"}
curl -i http://localhost:8080/ready    # 503

# Confirm the signals
curl -s http://localhost:8080/metrics | grep -E "db_errors_total|db_up|app_db_failure"

# Disarm
curl -X POST "http://localhost:8080/api/test/db-failure?enable=false"
curl http://localhost:8080/api/users   # 200 again
```

Modes: `connection_refused` (fails fast), `connection_timeout` (burns
`DB_FAILURE_DELAY_SECONDS` first, so latency rises too), `query_error`.

To start already-broken: `SIMULATE_DB_FAILURE=true` in `.env`. To disable the
runtime switch entirely: `ENABLE_TEST_CONTROLS=false` (the POST then returns
403).

Give it 5–10 minutes of failing traffic so Prometheus rates and Loki volume are
unambiguous.

## 15. Testing CPU stress

```bash
# 30 seconds of CPU load
curl "http://localhost:8080/api/cpu-stress?duration=30"

# Saturate several cores at once, then watch collateral latency
for i in 1 2 3; do curl -s "http://localhost:8080/api/cpu-stress?duration=60" & done
curl -w "\n%{time_total}s\n" http://localhost:8080/api/users
wait

# Bounds are enforced
curl -i "http://localhost:8080/api/cpu-stress?duration=99999"   # 400
curl -i "http://localhost:8080/api/cpu-stress?duration=0"       # 400

# Observe from the host
docker stats test-rca-app
curl -s http://localhost:8080/metrics | grep -E "process_cpu_seconds_total|app_cpu_stress"
```

The loop checks a monotonic deadline every iteration batch, so it always
terminates; concurrent runs are capped at the container's core count (HTTP 429
beyond that); and the compose CPU limit bounds host impact.

## 16. Testing application error

```bash
curl -i http://localhost:8080/api/error
curl -i "http://localhost:8080/api/error?kind=value"
curl -i "http://localhost:8080/api/error?kind=zero_division"
curl -i "http://localhost:8080/api/error?kind=bogus"       # 400, not a scenario

# The stack trace in the logs
docker compose logs app | grep '"level":"ERROR"' | tail -1

# 500s rise, database metrics do not
curl -s http://localhost:8080/metrics | grep 'endpoint="/api/error"'
curl -s http://localhost:8080/metrics | grep '^db_errors_total'
```

`kind` ∈ `runtime` (default), `value`, `key`, `zero_division`, `type`. The
response carries the exception class and `request_id` — never configuration.

## 17. Testing database latency

```bash
# One slow query
curl "http://localhost:8080/api/slow-query?seconds=10"

# Sustained latency for a realistic incident window
for i in $(seq 1 12); do curl -s "http://localhost:8080/api/slow-query?seconds=10" >/dev/null; done

# Default duration, and enforced bounds
curl http://localhost:8080/api/slow-query
curl -i "http://localhost:8080/api/slow-query?seconds=99999"   # 400

curl -s http://localhost:8080/metrics | grep 'db_query_duration_seconds_sum{operation="slow_query"}'
```

Implemented as `SELECT pg_sleep(n)`: read-only, takes no locks, self-terminating,
capped by `SLOW_QUERY_MAX_SECONDS`. Keep `DB_STATEMENT_TIMEOUT_MS` above that cap
or PostgreSQL cancels the sleep and the scenario turns into an error instead of
latency.

## 18. Expected Prometheus signals

| Signal | 1. DB failure | 2. CPU stress | 3. App exception | 4. DB latency |
|---|---|---|---|---|
| HTTP status | 503 | 200 | 500 | 200 |
| `http_request_errors_total` | Rising | Flat | Rising | Flat |
| `db_errors_total` | **Rising** | Flat | Flat | Flat |
| `db_up` | **0** | 1 | 1 | 1 |
| `db_query_duration_seconds` | n/a (failing) | Normal | Not involved | **Rising** |
| `process_cpu_seconds_total` | Normal | **At ceiling** | Normal | Normal |
| `http_request_duration_seconds` | Fast fail¹ | **Up everywhere** | Fast fail | **Up on DB routes** |

¹ Elevated in `connection_timeout` mode.

Each scenario has a distinct fingerprint — that separability is the point.

## 19. Expected Loki log signals

| Scenario | Level | Distinctive fields | Query |
|---|---|---|---|
| 1. DB failure | ERROR | `operation="select_users"`, `error_type="connection_refused"`, `db_target` | `\| json \| error_type=~"connection_.*"` |
| 2. CPU stress | WARNING | `scenario="cpu_stress"`, "CPU stress started/completed" | `\| json \| scenario="cpu_stress"` |
| 3. App exception | ERROR | `error_type="SimulatedApplicationError"`, `stack_trace` | `\| json \| endpoint="/api/error"` |
| 4. DB latency | WARNING | `operation="slow_query"`, `duration_ms > 1000` | `\| json \| duration_ms > 1000` |

The presence or absence of `stack_trace` distinguishes an application defect from
an infrastructure failure; `error_type` distinguishes the kinds of
infrastructure failure from each other.

## 20. EC2 deployment

Target: Ubuntu EC2 with Docker, Docker Compose, Git, and curl already installed.

```bash
git clone <repository-url>
cd test-rca-app

cp .env.example .env
nano .env            # set DATABASE_URL; set TRAFFIC_GENERATOR_ENABLED=true

docker compose build
docker compose up -d
```

Verify:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/metrics
docker compose logs -f app
./scripts/smoke_test.sh http://localhost:8080
```

Then, on the observability side:

1. Point Prometheus at `<EC2-private-IP>:8080` (or the container name if you
   joined the shared `observability` network) with a 15s scrape interval.
2. Configure Alloy to tail Docker logs and forward to Loki, keeping
   `container`/`service` labels so the LogQL queries in this README work.
3. Add both as Grafana datasources.
4. Confirm in Grafana: `up{job="test-rca-app"}` is 1, `http_requests_total` is
   increasing, and log lines are arriving.
5. Only then wire Grafana MCP (Phase 2).

Notes:

- **Security groups:** the app needs outbound access to the database port. Do
  not expose 8080 to the internet — keep it to the VPC/security group that
  Prometheus scrapes from.
- **Baseline first:** leave the traffic generator running 10–15 minutes before
  triggering any scenario.
- **Updating:** `git pull && docker compose build && docker compose up -d`
- **Log volume:** capped by the compose `json-file` options (20 MB × 5 files).

## 21. Running the tests

```bash
pip install -r requirements-dev.txt
```

Unit tests — no external dependencies. `tests/conftest.py` *forces* an in-memory
SQLite database, so an exported `DATABASE_URL` can never point the unit suite at
a real database:

```bash
pytest                       # 58 unit tests; integration tests skip
pytest tests/unit -v
```

Coverage: `/health`, `/ready`, `/api/users`, `/api/orders`, `/api/db-check`,
`/metrics` (required series present, no credentials, route-template labels
only), all four failure scenarios including their metric side effects, CPU
stress and slow-query bound validation, JSON log shape and secret redaction, and
configuration resolution.

Integration tests — opt in with a real PostgreSQL:

```bash
export INTEGRATION_DATABASE_URL='postgresql://user:pass@host:5432/db'
pytest -m integration
```

These build their own engine, create only the app's two tables, insert and then
delete a single probe row, and run a read-only `pg_sleep`.

## 22. Troubleshooting

**App exits at startup with a `ConfigError` about `DATABASE_URL`.**
No database configuration was found. Confirm `.env` exists, that
`docker-compose.yml`'s `env_file` points at it, and that either `DATABASE_URL` or
all five `DATABASE_*` values are set.

**Container is healthy but `/api/users` returns 503.**
Expected when the database is unreachable — `/health` intentionally ignores the
database. Diagnose:

```bash
curl -s http://localhost:8080/api/db-check
curl -s http://localhost:8080/api/test/db-failure     # is the simulation armed?
docker compose logs app | grep '"error_type"' | tail -5
```

If `enabled` is `true`, disarm it:
`curl -X POST "http://localhost:8080/api/test/db-failure?enable=false"`.
Otherwise check the security group / network path to the database port, whether
the database requires `?sslmode=require`, and the credentials.

**`connection refused` reaching the database from the container.**
`localhost` inside a container is the container. Use the database's real
hostname, or `host.docker.internal` for a database on the Docker host.

**`relation "users" does not exist`.**
`DB_AUTO_INIT` is `false`, or the database user lacks `CREATE TABLE`. Grant it,
or create the two tables manually.

**`process_cpu_seconds_total` and the other `process_*` metrics are missing.**
prometheus-client's process collector reads `/proc`, so these exist on Linux
only. In the Docker container (the deployment target) they are present; running
the app natively on Windows or macOS they are not, and `scripts/smoke_test.sh`
reports that as a `WARN`. Scenario 2's primary signal depends on them, so run
that scenario in the container.

**No metrics beyond `process_*` and `python_*`.**
Nothing has hit the app yet — counters appear on first use. Send a request, or
set `TRAFFIC_GENERATOR_ENABLED=true`.

**Prometheus shows the target as down.**
Check reachability (`curl http://<host>:8080/metrics` from the Prometheus host),
the security group, and whether Prometheus should be using the container name
rather than the host port.

**Logs are not JSON, or Loki shows nothing.**
The app only writes JSON to stdout; if you see plain text, something is running
an old image (`docker compose build --no-cache`). If stdout looks right but Loki
is empty, the problem is in Alloy's Docker discovery/relabeling, not here.
Confirm the source first:

```bash
docker compose logs app --tail 5 | tail -1 | python3 -m json.tool
```

**`/api/slow-query` returns 500 instead of a slow 200.**
`DB_STATEMENT_TIMEOUT_MS` is below the requested sleep, so PostgreSQL cancels it.
Raise it above `SLOW_QUERY_MAX_SECONDS × 1000`.

**`/api/cpu-stress` returns 429.**
The concurrency cap (one run per core) is saturated. Wait for a run to finish.

**CPU stress does not show up in Prometheus.**
The run is shorter than the scrape interval. Use `duration=60` with a 15s scrape,
and query `rate(process_cpu_seconds_total[1m])`.

**Metrics reset to zero.**
The container restarted — prometheus-client keeps counters in process memory.
Check `docker compose ps` and use `rate()`/`increase()`, which handle resets.

## 23. Security notes

- No credentials, tokens, or connection strings anywhere in the repository. A
  unit test scans `app/` for DSNs with embedded credentials.
- `.env` is gitignored (`.env.example` holds placeholders only) and excluded from
  the Docker image via `.dockerignore` — configuration arrives at runtime.
- `/health`, `/metrics`, and error responses never expose credentials, the
  `DATABASE_URL`, or arbitrary environment variables. Only `host:port` is ever
  surfaced, and only on `/api/db-check`.
- The JSON formatter redacts known sensitive keys and drops unrecognised fields.
- Container runs as non-root (uid 10001).
- Failure scenarios are bounded and non-destructive; there are no delete,
  truncate, or arbitrary-query endpoints. `ENABLE_TEST_CONTROLS=false` freezes
  the DB-failure switch.
- The app is a test instrument with deliberate failure endpoints and no
  authentication: keep port 8080 inside the VPC, never on the public internet.

## 24. Scope: what this repository is not

Phase 1 is the test environment — the application **and** its observability
stack. Explicitly **not** here:

- Grafana MCP integration (Phase 2)
- The LangGraph RCA agent, or any change to the Meyiconnect Agentic AI repo
- A custom MCP server
- Any CloudWatch or AWS SDK dependency — this app deliberately has none

---

# Part B — The observability stack

Everything below was added to this same repository and the same
`docker-compose.yml`. There is no second repository and no second compose file.

## 25. Existing application architecture

Unchanged by this work:

```
Internet → EC2 → Nginx :80 → test-rca-app :8080 → external PostgreSQL
```

- Nginx (`test-rca-nginx`) publishes **80:80** and reverse-proxies everything to
  `app:8080` (see `nginx.conf`).
- The application container (`test-rca-app`) publishes **no host port**; it is
  reachable only through Nginx and over the Docker network.
- The database is external and configured through `.env`.

The application's API, ports, healthcheck, logging driver, metrics
implementation, and failure scenarios were **not modified**. The only edit to
the two existing services was joining the `observability` network.

## 26. New observability architecture

```
                    ┌──────────────────────── EC2 ─────────────────────────┐
                    │                                                      │
 Internet ──:80──►  │  nginx ──► test-rca-app ──► external PostgreSQL      │
 Internet ──:3000─► │  grafana                                             │
                    │     │                                                │
                    │     ├──► prometheus ──scrape──► test-rca-app:8080    │
                    │     │          └───────scrape──► node-exporter:9100  │
                    │     │                                                │
                    │     └──► loki ◄──push── alloy ◄── Docker json-file   │
                    └──────────────────────────────────────────────────────┘
```

Two independent telemetry paths, both over the Docker network:

| Signal | Path |
|---|---|
| Metrics (app) | Prometheus **pulls** `http://test-rca-app:8080/metrics` every 15s |
| Metrics (host) | Prometheus **pulls** `http://node-exporter:9100/metrics` |
| Logs | app stdout → Docker `json-file` → Alloy tails → **pushes** to `http://loki:3100/loki/api/v1/push` |
| Query | Grafana → `http://prometheus:9090` and `http://loki:3100` |

Files added:

```
prometheus/prometheus.yml                                   # scrape config
loki/loki-config.yml                                        # single-node Loki
alloy/config.alloy                                          # Docker logs -> Loki
grafana/provisioning/datasources/datasources.yml            # auto datasources
grafana/provisioning/dashboards/dashboards.yml              # dashboard provider
grafana/provisioning/dashboards/test-rca-app-overview.json  # RCA dashboard
```

## 27. Why Prometheus

The application already exposed `/metrics`, but nothing was storing it — each
scrape was a point-in-time snapshot with no history. An RCA agent cannot reason
about an incident without a time series: "did database errors *increase*", "was
latency *worse* than baseline", "when did this *start*". Prometheus provides
that history and the PromQL to query it, and it is the metrics backend Grafana
MCP will read through in Phase 2.

Retention is 15 days (`--storage.tsdb.retention.time=15d`) — more than a test
environment needs, and it keeps the volume small.

## 28. Why Loki

Metrics say *that* something broke; logs say *what* broke. The application's
structured JSON logs already contain the decisive fields — `error_type`,
`stack_trace`, `operation`, `db_target`, `duration_ms` — but they only lived in
Docker's local log files: reachable one container at a time through
`docker logs`, and not queryable over a time range.

Loki makes them queryable with LogQL, and its label model matches the metric
labels, so the agent can pivot from a Prometheus anomaly to the exact log lines
in the same window. That is what turns "500s increased" into "`ValueError` at
`app/routes/test_failures.py:145`".

## 29. Why Grafana Alloy

Something has to move logs from Docker into Loki. Alloy is Grafana's current
collector — the **deprecated Grafana Agent is deliberately not used**.

Alloy fits because it discovers containers through the Docker API rather than
needing per-container configuration: new containers are picked up automatically,
and the app keeps its existing `json-file` driver, so `docker logs test-rca-app`
still works exactly as before. Alloy only reads; it changes nothing about how the
application logs.

## 30. Why Grafana

Grafana is the single query surface over both datasources, and the only
observability component exposed outside the host. Two reasons it matters here:

1. **Phase 2 depends on it.** Grafana MCP talks to Grafana, not to Prometheus and
   Loki directly. Provisioned datasources with stable UIDs (`prometheus`, `loki`)
   are exactly what the MCP server will enumerate.
2. **Human validation.** Before trusting an agent's RCA, a person needs to
   confirm the telemetry actually tells the story. That is what the provisioned
   dashboard is for.

## 31. Why node_exporter

The CPU-stress scenario is a *host* resource problem, and the application cannot
credibly report on its own resource exhaustion. Its `/metrics` does expose
`process_cpu_seconds_total` for its own process, but nothing about the EC2
instance: total CPU across cores, memory pressure, disk fill, network
throughput, or load average.

node_exporter supplies the host view, which is what separates "the application is
busy" from "the instance is saturated" — a distinction the RCA agent has to make.
It needs three read-only host mounts to see the host rather than its own
namespace:

| Mount | Purpose |
|---|---|
| `/proc:/host/proc:ro` | CPU, memory, load, network counters |
| `/sys:/host/sys:ro` | block devices, thermal, network device details |
| `/:/rootfs:ro` | filesystem capacity/usage for real mountpoints |

It also runs with `pid: host` for accurate host process metrics. It is **not**
privileged and publishes **no host port**.

## 32. Port usage

| Service | Host binding | Reachable from | Notes |
|---|---|---|---|
| nginx | `80:80` | Internet | **Unchanged.** The application's only public entry point |
| grafana | `3000:3000` | Internet | The observability UI |
| prometheus | `127.0.0.1:9090:9090` | EC2 localhost only | Use an SSH tunnel |
| loki | `127.0.0.1:3100:3100` | EC2 localhost only | Auth is disabled — must not be public |
| alloy | *none* | Docker network only | Diagnostics on `:12345` inside the network |
| node-exporter | *none* | Docker network only | Never publicly exposed |
| test-rca-app | *none* | Docker network only | Through Nginx, as before |

To reach Prometheus or Loki from a workstation, tunnel rather than publish:

```bash
ssh -L 9090:127.0.0.1:9090 -L 3100:127.0.0.1:3100 ubuntu@EC2_PUBLIC_IP
```

Security group: open **80** and **3000** only. Do not open 9090, 3100, or 9100.

## 33. Docker network

One Compose-managed bridge network, `observability`, shared by all seven
services:

```yaml
networks:
  observability:
    driver: bridge
```

Compose creates it on `up` and removes it on `down` — **no `docker network
create` step on EC2**, and it is not an external network. Every container
resolves every other by name, which is why Prometheus can scrape
`test-rca-app:8080` and Alloy can push to `loki:3100`.

The app service also registers an explicit alias:

```yaml
networks:
  observability:
    aliases:
      - test-rca-app
```

Docker already resolves `container_name`, but the alias means the Prometheus
target keeps working even if `container_name` ever changes.

## 34. Starting the complete stack

```bash
cd ~/agentic-ai-test-env
git pull
cp .env.example .env
docker compose up -d --build
```

`cp .env.example .env` is first-time only — then edit it. Set at least
`DATABASE_URL` and `GRAFANA_ADMIN_PASSWORD`, and keep
`TRAFFIC_GENERATOR_ENABLED=true` so Prometheus and Loki always have a baseline.

Startup takes about 30–45 seconds for everything to report healthy. The
application does **not** wait for the observability stack — it has no
`depends_on` pointing at it, so a broken Prometheus or Loki cannot stop the app
from serving traffic.

## 35. Stopping the stack

```bash
docker compose down
```

That stops all containers but **keeps** the named volumes
(`prometheus_data`, `loki_data`, `grafana_data`, `alloy_data`) — verified. Other
options:

```bash
docker compose stop              # just stop, keep containers
docker compose restart grafana   # restart a single service
```

Only this deletes the data:

```bash
docker compose down -v           # DESTROYS metrics history, logs, dashboards
```

## 36. Checking containers

```bash
docker compose ps
docker ps
```

Expected — seven containers:

| Container | Expected status |
|---|---|
| `test-rca-nginx` | Up |
| `test-rca-app` | Up (healthy) |
| `prometheus` | Up (healthy) |
| `loki` | Up (healthy) |
| `grafana` | Up (healthy) |
| `alloy` | Up |
| `node-exporter` | Up |

`alloy` and `node-exporter` show no health status because their images ship no
healthcheck tooling; verify them through §41 and the Prometheus targets page
instead.

Logs for any component:

```bash
docker compose logs -f prometheus
docker compose logs -f alloy
docker logs test-rca-app
```

## 37. Accessing Grafana

```
http://EC2_PUBLIC_IP:3000
```

The provisioned dashboard is under **Dashboards → Meyiconnect RCA → "Meyiconnect
RCA — Application & Host Overview"**, or directly:

```
http://EC2_PUBLIC_IP:3000/d/meyiconnect-rca-overview
```

Port 3000 must be open in the EC2 security group. Restrict it to your own IP
range where possible.

## 38. Grafana login

Credentials come from `.env`, never from `docker-compose.yml`:

```bash
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=CHANGE_ME
```

Change the password before deploying — this is the one publicly reachable
observability endpoint. If unset, Grafana falls back to `admin`/`admin`.

Sign-up is disabled (`GF_USERS_ALLOW_SIGN_UP=false`), so the admin account is the
only way in.

The Grafana container deliberately does **not** receive `.env` as an `env_file`;
only the two `GRAFANA_*` variables are interpolated in. The PostgreSQL credential
is never passed to Grafana, Prometheus, or Loki.

Note that Grafana persists the password in `grafana_data` after first boot, so
changing `GRAFANA_ADMIN_PASSWORD` later has no effect on an existing volume —
reset it with `grafana-cli` inside the container, or recreate the volume.

## 39. Prometheus verification

```bash
curl http://localhost:9090/-/ready
```

Expect `Prometheus Server is Ready.`

Every scrape target and its health:

```bash
curl -s http://localhost:9090/api/v1/targets?state=active | python3 -m json.tool | grep -E '"job"|"health"|scrapeUrl'
```

Expect all five `up`:

```
alloy          http://alloy:12345/metrics         up
loki           http://loki:3100/metrics           up
node-exporter  http://node-exporter:9100/metrics  up
prometheus     http://localhost:9090/metrics      up
test-rca-app   http://test-rca-app:8080/metrics   up
```

Confirm it is scraping the app by Docker DNS rather than a host port or public
IP:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=up{job="test-rca-app"}'
```

The Prometheus UI (through an SSH tunnel) is at `http://localhost:9090/targets`.

## 40. Loki verification

```bash
curl http://localhost:3100/ready
```

Expect `ready`. For roughly the first 30 seconds after start it reports not
ready, which is normal.

Which labels arrived:

```bash
curl -s http://localhost:3100/loki/api/v1/labels
```

Expect `container`, `service`, `environment`, `component`, `project` (plus
Loki's own `service_name` and `detected_level`).

Which containers are shipping logs:

```bash
curl -s http://localhost:3100/loki/api/v1/label/container/values
```

Pull actual application log lines:

```bash
curl -s -G http://localhost:3100/loki/api/v1/query_range --data-urlencode 'query={service="test-rca-app"}' --data-urlencode 'limit=5'
```

## 41. Alloy verification

Alloy publishes no host port, so check it from inside the network:

```bash
docker compose logs --tail 30 alloy
docker exec alloy wget -qO- http://127.0.0.1:12345/metrics | head -20
```

The decisive test is whether logs are actually landing in Loki (§40). Prometheus
also scrapes Alloy as job `alloy`, which shows whether the pipeline is healthy or
silently stalled:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(loki_write_sent_entries_total[5m]))'
```

Above zero means Alloy is actively delivering to Loki. Silence in Loki *plus*
zero here points at Alloy or the Docker socket; silence in Loki with a non-zero
value here points at Loki.

## 42. Application metrics verification

Through Nginx (unchanged behaviour):

```bash
curl http://localhost/metrics
```

Through Prometheus, confirming it stored them:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=sum(rate(http_requests_total{job="test-rca-app"}[5m]))by(endpoint)'
curl -s 'http://localhost:9090/api/v1/query?query=db_up{job="test-rca-app"}'
```

Host metrics:

```bash
curl -s 'http://localhost:9090/api/v1/query?query=node_load1{job="node-exporter"}'
```

Every dashboard panel uses a metric that actually exists — each expression was
verified against Prometheus. Nothing is invented. `db_errors_total` is absent
until the first database failure occurs, which is correct: a Prometheus counter
only appears once it has been incremented.

## 43. Application log verification

The pipeline is `test-rca-app → Docker json-file → Alloy → Loki → Grafana`, and
the first hop is unchanged:

```bash
docker logs test-rca-app --tail 5
```

```bash
docker logs test-rca-app --tail 1 | python3 -m json.tool
```

Then in Loki, via Grafana **Explore → Loki**:

```logql
{service="test-rca-app"}
{service="test-rca-app"} | json | level="ERROR"
{service="test-rca-app"} | json | operation="select_users"
{service="test-rca-app"} | json | duration_ms > 1000
sum(count_over_time({service="test-rca-app"} | json | __error__="" [1m])) by (level)
```

Note the split: **labels** are the bounded stream selectors (`service`,
`container`, `environment`, `component`, `project`, `stream`), while
high-cardinality fields (`request_id`, `endpoint`, `error_type`, `duration_ms`)
stay inside the JSON body and are filtered with `| json` after selection. That is
deliberate — promoting `request_id` to a Loki label would create one stream per
request and fall over.

## 44. RCA failure scenario testing

All four scenarios, driven through Nginx and observed in the stack. Let the
baseline run about 10 minutes first.

Drive **sustained** load rather than a single request: if a counter's entire jump
happens between two scrapes, `rate()` and `increase()` legitimately report 0,
because Prometheus has only ever seen that counter at its post-jump value.

### Scenario 1 — database failure

```bash
curl -X POST "http://localhost/api/test/db-failure?enable=true&mode=connection_refused"
```

```bash
for i in $(seq 1 60); do curl -s -o /dev/null http://localhost/api/users; curl -s -o /dev/null http://localhost/api/orders; sleep 1; done
```

```bash
curl -X POST "http://localhost/api/test/db-failure?enable=false"
```

Prometheus:

```promql
sum(rate(db_errors_total{job="test-rca-app"}[2m])) by (operation, error_type)
sum(rate(http_requests_total{job="test-rca-app",status="503"}[2m])) by (endpoint)
db_up{job="test-rca-app"}
```

Loki:

```logql
{service="test-rca-app"} | json | error_type="connection_refused"
```

Measured during validation: `db_errors_total` for `select_users` reached 145
while `http_requests_total{endpoint="/api/users",status="503"}` also reached 145
— the two move in exact lockstep, which is the correlation the agent needs.

### Scenario 2 — CPU stress

```bash
curl "http://localhost/api/cpu-stress?duration=60"
```

Prometheus:

```promql
100 - (avg(rate(node_cpu_seconds_total{job="node-exporter",mode="idle"}[1m])) * 100)
sum(rate(process_cpu_seconds_total{job="test-rca-app"}[1m])) * 100
```

Loki:

```logql
{service="test-rca-app"} | json | scenario="cpu_stress"
```

Measured: host CPU peaked at 41.7% and app process CPU at 31.8% during a 15s
run, with "CPU stress started" and "CPU stress completed" bracketing the window
in Loki.

### Scenario 3 — application error

```bash
for i in $(seq 1 30); do curl -s -o /dev/null http://localhost/api/error; sleep 1; done
```

Prometheus — the 500s stay scoped to one endpoint, and `db_errors_total` does not
move:

```promql
sum(rate(http_requests_total{job="test-rca-app",status="500"}[2m])) by (endpoint)
```

Loki — the stack trace is the attribution:

```logql
{service="test-rca-app"} | json | error_type="SimulatedApplicationError"
```

### Scenario 4 — database latency

```bash
for i in $(seq 1 12); do curl -s -o /dev/null "http://localhost/api/slow-query?seconds=10"; done
```

Prometheus:

```promql
histogram_quantile(0.95, sum(rate(db_query_duration_seconds_bucket{job="test-rca-app"}[5m])) by (le, operation))
```

Loki:

```logql
{service="test-rca-app"} | json | operation="slow_query"
```

The four scenarios stay distinguishable on metrics alone; the full matrix is in
[§18](#18-expected-prometheus-signals) and
[docs/failure-scenarios.md](docs/failure-scenarios.md).

## 45. Observability troubleshooting

**Prometheus target `test-rca-app` is DOWN.**
Check both containers are on the `observability` network, then test resolution
from inside Prometheus:

```bash
docker inspect test-rca-app --format '{{json .NetworkSettings.Networks}}'
```

```bash
docker exec prometheus wget -qO- http://test-rca-app:8080/health
```

If the name does not resolve, the app service is missing the network or the
alias. Never "fix" this by switching the target to an EC2 IP or `localhost` —
inside the Prometheus container, `localhost` is Prometheus.

**No logs in Loki at all.**
Work the pipeline in order — app, then Alloy, then Loki:

```bash
docker logs test-rca-app --tail 3
```

```bash
docker compose logs --tail 30 alloy
```

```bash
docker exec alloy wget -qO- http://loki:3100/ready
```

```bash
curl -s http://localhost:3100/loki/api/v1/label/container/values
```

The usual cause is Alloy being unable to read `/var/run/docker.sock`.

**Alloy restarts, or logs "permission denied" on the Docker socket.**
The socket is root-owned on the host, which is why Alloy runs as `root`. Confirm:

```bash
docker inspect alloy --format '{{.Config.User}} {{json .Mounts}}'
```

If you switched Alloy to non-root, add the host's docker group gid with
`group_add` instead.

**Loki rejects entries as too old.**
`reject_old_samples_max_age` is 168h, so logs older than 7 days are dropped.
Expected after a long container downtime; not a fault.

**Grafana shows "Datasource not found" on the dashboard.**
The dashboard references datasources by uid (`prometheus`, `loki`). If they were
renamed or hand-edited, re-provision and check:

```bash
docker compose restart grafana
```

```bash
curl -s -u admin:YOUR_PASSWORD http://localhost:3000/api/datasources
```

**node-exporter metrics missing or implausible.**
It needs the three read-only host mounts and only reports meaningfully on a Linux
host. On Docker Desktop for Windows/macOS it reports the Linux VM, not your
workstation.

**Panels empty but all targets up.**
Either no traffic is being generated (set `TRAFFIC_GENERATOR_ENABLED=true`, or
run `scripts/generate_traffic.sh`), or the counter has not been incremented yet —
`db_errors_total` genuinely does not exist until the first database failure.

**Metrics reset to zero.**
The app container restarted; prometheus-client keeps counters in process memory.
Use `rate()` / `increase()`, which handle counter resets.

## 46. Future Grafana MCP integration

Out of scope for Phase 1 — no MCP code is in this repository. What Phase 2 will
attach to:

```
LangGraph RCA Agent → Grafana MCP → Grafana → Prometheus (metrics)
                                            → Loki       (logs)
```

Phase 1 deliberately leaves it ready:

- Grafana is reachable on a stable port with provisioned datasources at fixed
  UIDs (`prometheus`, `loki`), which is what an MCP server enumerates.
- Both telemetry paths are verified end to end, so a Phase-2 failure can be
  attributed to the agent or to MCP rather than to missing data.
- Metric and log labels are low-cardinality and consistent, so the agent can
  correlate a metric anomaly with log evidence in the same window.
- All four failure scenarios have a **known correct root cause**, so the agent's
  RCA output can be graded rather than merely inspected.

When Phase 2 starts, the likely additions are a Grafana service account token for
MCP authentication and MCP server configuration in the Meyiconnect Agentic AI
repository — not here.
