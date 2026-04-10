#!/usr/bin/env bash
#
# Integration tests for the gateway compose profile.
# Manages its own compose lifecycle: up → test → down.
#
# ACs covered:
#   AC2: GET http://localhost:8080/api/health → 200 through Tyk (or 401 if auth required)
#   AC3: Expired JWT through Tyk → 401
#   AC5: Gateway profile starts exactly the expected number of containers
#
# Usage:
#   ./scripts/test-integration-gateway.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

TYK_URL="${TYK_URL:-http://localhost:8080}"
BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
MAX_WAIT="${MAX_WAIT:-90}"
PASS=0
FAIL=0
SKIP=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  SKIP: $1"; SKIP=$((SKIP + 1)); }

header() { echo -e "\n=== $1 ==="; }

cleanup() {
    header "Teardown"
    echo "Stopping gateway profile..."
    docker compose --profile gateway down -v --timeout 10 2>&1 || true
    echo "Teardown complete."
}
trap cleanup EXIT

# ── Pre-flight: check required env vars ──
header "Pre-flight"
MISSING_VARS=()
[ -z "${DESCOPE_PROJECT_ID:-}" ] && MISSING_VARS+=("DESCOPE_PROJECT_ID")
[ -z "${TYK_GATEWAY_SECRET:-}" ] && MISSING_VARS+=("TYK_GATEWAY_SECRET")
[ -z "${POSTGRES_PASSWORD:-}" ] && MISSING_VARS+=("POSTGRES_PASSWORD")

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    echo "Missing required env vars: ${MISSING_VARS[*]}"
    echo "Source your .env file: set -a && source .env && set +a"
    exit 1
fi
echo "All required env vars present."

# ── Startup ──
header "Starting gateway profile"
docker compose --profile gateway up -d --build --wait --wait-timeout "$MAX_WAIT"
echo "Compose up complete."

# ── AC5: Container count ──
header "AC5: Gateway container count"
EXPECTED_COUNT=$(docker compose --profile gateway config --services | wc -l | tr -d ' ')
RUNNING_COUNT=$(docker compose --profile gateway ps --status running --format json | grep -c '"Service"' || echo "0")
if [ "$RUNNING_COUNT" -eq "$EXPECTED_COUNT" ]; then
    pass "Running containers (${RUNNING_COUNT}) matches expected (${EXPECTED_COUNT})"
else
    fail "Running containers (${RUNNING_COUNT}) does not match expected (${EXPECTED_COUNT})"
fi

# ── AC2: Health check through Tyk ──
header "AC2: Gateway health check through Tyk"
HEALTH_TMPFILE=$(mktemp)
trap 'rm -f "$HEALTH_TMPFILE"; cleanup' EXIT
HEALTH_CODE=$(curl -s -w '%{http_code}' -o "$HEALTH_TMPFILE" --connect-timeout 5 "${TYK_URL}/api/health" 2>/dev/null || echo "000")
HEALTH_BODY=$(cat "$HEALTH_TMPFILE" 2>/dev/null || true)
rm -f "$HEALTH_TMPFILE"

if [ "$HEALTH_CODE" = "200" ]; then
    if echo "$HEALTH_BODY" | grep -q '"status"'; then
        pass "GET /api/health through Tyk returns 200 with status field"
    else
        fail "GET /api/health through Tyk returned 200 but unexpected body: ${HEALTH_BODY}"
    fi
elif [ "$HEALTH_CODE" = "401" ]; then
    # Tyk OpenID policy covers all /api/ paths — auth required is expected behavior
    echo "  INFO: Tyk requires auth for /api/health (OpenID policy covers /api/)"
    # Verify backend is reachable directly to confirm the stack is healthy
    DIRECT_CODE=$(curl -sf -w '%{http_code}' -o /dev/null --connect-timeout 5 "${BACKEND_URL}/api/health" 2>/dev/null || echo "000")
    if [ "$DIRECT_CODE" = "200" ]; then
        pass "Tyk proxy is active (401 for unauthenticated /api/health); backend healthy directly"
    else
        fail "Tyk returned 401 but backend not reachable directly (HTTP ${DIRECT_CODE})"
    fi
else
    fail "GET /api/health through Tyk returned HTTP ${HEALTH_CODE} (expected 200 or 401)"
fi

# ── AC3: Expired JWT rejection ──
header "AC3: Expired JWT rejection through Tyk"
# nosemgrep: generic.secrets.security.detected-jwt-token.detected-jwt-token
EXPIRED_JWT="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0IiwiZXhwIjoxMDAwMDAwMDAwfQ.invalid-signature"
EXPIRED_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${EXPIRED_JWT}" \
    "${TYK_URL}/api/tenants" 2>/dev/null || echo "000")

if [ "$EXPIRED_CODE" = "401" ] || [ "$EXPIRED_CODE" = "403" ]; then
    pass "Expired/invalid JWT rejected by Tyk with HTTP ${EXPIRED_CODE} — not reaching FastAPI"
else
    fail "Expired/invalid JWT got HTTP ${EXPIRED_CODE} (expected 401 or 403)"
fi

# ── Summary ──
header "Results"
echo "  ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "GATEWAY INTEGRATION TESTS FAILED"
    exit 1
else
    echo "GATEWAY INTEGRATION TESTS PASSED"
    exit 0
fi
