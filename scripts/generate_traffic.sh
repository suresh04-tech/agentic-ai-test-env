#!/usr/bin/env bash
# Generate baseline HTTP traffic against a running test-rca-app.
#
# The app has a built-in generator (TRAFFIC_GENERATOR_ENABLED=true); this script
# is the external equivalent, useful when you want load from outside the
# container or a different request rate for a while.
#
# Usage:
#   ./scripts/generate_traffic.sh [BASE_URL] [DURATION_SECONDS] [REQUESTS_PER_SECOND]
#
# Example:
#   ./scripts/generate_traffic.sh http://localhost:8080 600 5

set -euo pipefail

BASE_URL="${1:-http://localhost:8080}"
DURATION="${2:-300}"
RPS="${3:-2}"

# Read endpoints only. Failure scenarios stay operator-triggered on purpose.
PATHS=(
  "/api/users"
  "/api/users?limit=10"
  "/api/orders"
  "/api/orders?status=paid"
  "/api/orders?limit=5"
  "/health"
)

SLEEP=$(awk "BEGIN {printf \"%.3f\", 1/$RPS}")
DEADLINE=$(( $(date +%s) + DURATION ))
COUNT=0

echo "Generating ~${RPS} req/s against ${BASE_URL} for ${DURATION}s (Ctrl-C to stop)"

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  path="${PATHS[$((RANDOM % ${#PATHS[@]}))]}"
  curl -s -o /dev/null -w "" "${BASE_URL}${path}" || true
  COUNT=$((COUNT + 1))
  if [ $((COUNT % 50)) -eq 0 ]; then
    echo "  ${COUNT} requests sent"
  fi
  sleep "$SLEEP"
done

echo "Done: ${COUNT} requests sent"
