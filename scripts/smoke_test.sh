#!/usr/bin/env bash
# Post-deployment smoke test: verifies every endpoint and every failure scenario
# against a running instance, then leaves the app in a clean state.
#
# Usage:
#   ./scripts/smoke_test.sh [BASE_URL]

set -uo pipefail

BASE_URL="${1:-http://localhost:8080}"
FAILURES=0

check() {
  local description="$1" expected="$2" url="$3" method="${4:-GET}"
  local actual
  actual=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "$url")
  if [ "$actual" = "$expected" ]; then
    printf '  PASS  %-46s %s\n' "$description" "$actual"
  else
    printf '  FAIL  %-46s expected %s, got %s\n' "$description" "$expected" "$actual"
    FAILURES=$((FAILURES + 1))
  fi
}

echo "Smoke testing ${BASE_URL}"
echo
echo "Core endpoints"
check "GET /health"                200 "${BASE_URL}/health"
check "GET /ready"                 200 "${BASE_URL}/ready"
check "GET /metrics"               200 "${BASE_URL}/metrics"
check "GET /api/users"             200 "${BASE_URL}/api/users"
check "GET /api/orders"            200 "${BASE_URL}/api/orders"
check "GET /api/db-check"          200 "${BASE_URL}/api/db-check"

echo
echo "Metrics content"
METRICS=$(curl -s "${BASE_URL}/metrics")
for metric in http_requests_total http_request_duration_seconds \
              http_request_errors_total db_queries_total \
              db_query_duration_seconds db_errors_total db_up \
              app_records_fetched_total app_info; do
  if grep -q "^${metric}" <<<"$METRICS" || grep -q "^# HELP ${metric}" <<<"$METRICS"; then
    printf '  PASS  %-46s present\n' "$metric"
  else
    printf '  FAIL  %-46s MISSING\n' "$metric"
    FAILURES=$((FAILURES + 1))
  fi
done

# prometheus-client's process collector reads /proc, so these exist only on
# Linux. That is the deployment target (Docker on EC2), but this script may be
# run from a Windows or macOS workstation — so a miss here is a warning, not a
# failure. Scenario 2 depends on these, so it is worth flagging loudly.
if grep -q "^process_cpu_seconds_total" <<<"$METRICS"; then
  printf '  PASS  %-46s present\n' "process_cpu_seconds_total"
else
  printf '  WARN  %-46s absent (Linux-only; expected in the container)\n' \
    "process_cpu_seconds_total"
fi

echo
echo "Scenario 3 — application error"
check "GET /api/error"             500 "${BASE_URL}/api/error"
check "GET /api/error?kind=value"  500 "${BASE_URL}/api/error?kind=value"
check "GET /api/error?kind=bogus"  400 "${BASE_URL}/api/error?kind=bogus"

echo
echo "Scenario 4 — database latency"
check "GET /api/slow-query"        200 "${BASE_URL}/api/slow-query?seconds=2"
check "slow-query over cap"        400 "${BASE_URL}/api/slow-query?seconds=99999"
check "slow-query zero"            400 "${BASE_URL}/api/slow-query?seconds=0"

echo
echo "Scenario 2 — CPU stress (bounded)"
check "GET /api/cpu-stress?duration=2"  200 "${BASE_URL}/api/cpu-stress?duration=2"
check "cpu-stress over cap"             400 "${BASE_URL}/api/cpu-stress?duration=99999"
check "cpu-stress zero"                 400 "${BASE_URL}/api/cpu-stress?duration=0"

echo
echo "Scenario 1 — database failure"
check "arm switch"                 200 "${BASE_URL}/api/test/db-failure?enable=true" POST
check "users during outage"        503 "${BASE_URL}/api/users"
check "orders during outage"       503 "${BASE_URL}/api/orders"
check "health stays healthy"       200 "${BASE_URL}/health"
check "ready reports not ready"    503 "${BASE_URL}/ready"
check "disarm switch"              200 "${BASE_URL}/api/test/db-failure?enable=false" POST
check "users recovered"            200 "${BASE_URL}/api/users"

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "All checks passed."
  exit 0
fi
echo "${FAILURES} check(s) failed."
exit 1
