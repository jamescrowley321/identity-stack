#!/usr/bin/env bash
#
# Integration tests for the standalone (default) compose profile.
# Manages its own compose lifecycle: up → test → down.
#
# ACs covered:
#   AC1: GET http://localhost:8000/api/health → 200
#   AC4: Standalone profile starts exactly the expected number of containers
#
# Usage:
#   ./scripts/test-integration-standalone.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
MAX_WAIT="${MAX_WAIT:-60}"
PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

header() { echo -e "\n=== $1 ==="; }

cleanup() {
    header "Teardown"
    echo "Stopping standalone profile..."
    docker compose down -v --timeout 10 2>&1 || true
    echo "Teardown complete."
}
trap cleanup EXIT

# ── Startup ──
header "Starting standalone profile"
docker compose up -d --build --wait --wait-timeout "$MAX_WAIT"
echo "Compose up complete."

# ── AC4: Container count ──
header "AC4: Standalone container count"
EXPECTED_COUNT=$(docker compose config --services | wc -l | tr -d ' ')
RUNNING_COUNT=$(docker compose ps --status running --format json | grep -c '"Service"' || echo "0")
if [ "$RUNNING_COUNT" -eq "$EXPECTED_COUNT" ]; then
    pass "Running containers (${RUNNING_COUNT}) matches expected (${EXPECTED_COUNT})"
else
    fail "Running containers (${RUNNING_COUNT}) does not match expected (${EXPECTED_COUNT})"
fi

# ── AC1: Health check ──
header "AC1: Standalone health check"
HEALTH_TMPFILE=$(mktemp)
trap 'rm -f "$HEALTH_TMPFILE"; cleanup' EXIT
HEALTH_CODE=$(curl -sf -w '%{http_code}' -o "$HEALTH_TMPFILE" --connect-timeout 5 "${BACKEND_URL}/api/health" 2>/dev/null || echo "000")
HEALTH_BODY=$(cat "$HEALTH_TMPFILE" 2>/dev/null || true)
rm -f "$HEALTH_TMPFILE"

if [ "$HEALTH_CODE" = "200" ]; then
    if echo "$HEALTH_BODY" | grep -q '"status"'; then
        pass "GET /api/health returns 200 with status field"
    else
        fail "GET /api/health returned 200 but unexpected body: ${HEALTH_BODY}"
    fi
else
    fail "GET /api/health returned HTTP ${HEALTH_CODE} (expected 200)"
fi

# ── Summary ──
header "Results"
echo "  ${PASS} passed, ${FAIL} failed"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "STANDALONE INTEGRATION TESTS FAILED"
    exit 1
else
    echo "STANDALONE INTEGRATION TESTS PASSED"
    exit 0
fi
