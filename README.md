# test-rca-app

A controlled observability / RCA test application for the **Meyiconnect Agentic
AI** project (GENAIPR-002, Phase 1).

It looks and behaves like a small customer web application — FastAPI over
PostgreSQL — but its real job is to emit **Prometheus metrics** and **structured
JSON logs** that tell a coherent story, and to fail **on demand, safely, in four
distinct ways**.

---

## Table of contents

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

Phase 1 (this repo) prepares the environment. Phase 2 wires Grafana MCP into the
LangGraph agent — no MCP, agent, or Grafana code lives here.

## 3. Architecture

Phase-1 target environment on a single EC2 instance:

```
                             AWS EC2
                                |
                +---------------+----------------+
                |                                |
          Test Application                  Observability stack
          (this repository)                 (separate repository)
                |                                |
        +-------+-------+              +---------+---------+
        |               |              |         |         |
    /metrics        stdout logs    Prometheus  Alloy      Loki
        |               |              ^         |         ^
        |               +--------------|---------+---------+
        |                              |
        +------------------------------+
                                       |
                                    Grafana
                                       |
                                  Grafana MCP
                                       |
                              Phase 2: LangGraph RCA
```

Data flow out of this application:

| Path | Consumer |
|---|---|
| `GET /metrics` (HTTP pull) | Prometheus scrape |
| stdout → Docker `json-file` log driver | Grafana Alloy → Loki |

The app connects **out** to an external PostgreSQL database. Prometheus, Loki,
Alloy, and Grafana are deliberately **not** in this repository's
`docker-compose.yml` — they are a separate stack.

## 4. Project structure

```
test-rca-app/
├── app/
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
├── Dockerfile
├── docker-compose.yml
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

The compose file runs **only the application**. It publishes `${APP_PORT:-8080}`,
caps the container at 1.5 CPU / 512 MB (so CPU saturation is visible but
bounded), and configures the `json-file` log driver with `service`,
`environment`, and `component` labels for Alloy to promote into Loki labels.

**Letting Prometheus scrape the app.** Either point Prometheus at the host port
(`<EC2-private-IP>:8080`), or put both stacks on one Docker network:

```bash
docker network create observability
```

then uncomment the `networks` blocks in `docker-compose.yml`, add the same
external network to the observability stack, and scrape
`http://test-rca-app:8080/metrics` by container name.

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

Phase 1 is the target application only. Explicitly **not** here:

- Grafana MCP integration (Phase 2)
- The LangGraph RCA agent, or any change to the Meyiconnect Agentic AI repo
- A custom MCP server
- Prometheus, Loki, Alloy, or Grafana deployment (separate observability stack)
- Any CloudWatch or AWS SDK dependency — this app deliberately has none
#   t e s t - r c a - a p p  
 # agentic-ai-test-env
