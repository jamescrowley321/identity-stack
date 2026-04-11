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
    local exit_code=$?
    header "Teardown"
    if [ "$exit_code" -ne 0 ] || [ "${FAIL:-0}" -gt 0 ]; then
        echo "Test run failed — dumping container logs before teardown:"
        docker compose logs --tail=200 2>&1 || true
    fi
    echo "Stopping standalone profile..."
    docker compose down -v --timeout 10 2>&1 || true
    echo "Teardown complete."
}
trap cleanup EXIT

# ── Pre-flight: env vars ──
header "Pre-flight"
MISSING_VARS=()
[ -z "${POSTGRES_PASSWORD:-}" ] && MISSING_VARS+=("POSTGRES_PASSWORD")

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
    echo "Missing required env vars: ${MISSING_VARS[*]}"
    echo "Source your .env file: set -a && source .env && set +a"
    exit 1
fi

# Compose interpolates all services (including gateway-profiled ones).
# Provide dummy values for gateway-only vars so standalone compose up succeeds.
export TYK_GATEWAY_SECRET="${TYK_GATEWAY_SECRET:-unused}"
export DESCOPE_PROJECT_ID="${DESCOPE_PROJECT_ID:-unused}"

echo "All required env vars present."

# ── Startup ──
header "Starting standalone profile"
if ! docker compose up -d --build --wait --wait-timeout "$MAX_WAIT"; then
    echo "ERROR: Compose up failed. Container logs:"
    docker compose logs --tail=50 || true
    exit 1
fi
echo "Compose up complete."

# ── AC: Backend DEPLOYMENT_MODE is standalone (story 3.2 wiring) ──
header "AC: Backend DEPLOYMENT_MODE is standalone"
set +e
ACTUAL_MODE=$(docker compose exec -T backend printenv DEPLOYMENT_MODE 2>/dev/null | tr -d '\r\n')
set -e
if [ "$ACTUAL_MODE" = "standalone" ]; then
    pass "Backend container has DEPLOYMENT_MODE=standalone"
else
    fail "Backend container has DEPLOYMENT_MODE='${ACTUAL_MODE}', expected 'standalone'"
fi

# ── AC4: Container count ──
header "AC4: Standalone container count"
EXPECTED_COUNT=$(docker compose config --services | wc -l | tr -d ' ')
RUNNING_COUNT=$(docker compose ps --status running -q | wc -l | tr -d ' ')
if [ "$RUNNING_COUNT" -eq "$EXPECTED_COUNT" ]; then
    pass "Running containers (${RUNNING_COUNT}) matches expected (${EXPECTED_COUNT})"
else
    fail "Running containers (${RUNNING_COUNT}) does not match expected (${EXPECTED_COUNT})"
fi

# ── AC1: Health check ──
header "AC1: Standalone health check"
HEALTH_TMPFILE=$(mktemp)
HEALTH_CODE=""
HEALTH_BODY=""
# `docker compose up --wait` only waits for the container to be running; with a
# backend healthcheck defined in compose it waits for healthy, but we still
# retry here as defense-in-depth for slow uvicorn cold starts.
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    set +e
    HEALTH_CODE=$(curl -s -o "$HEALTH_TMPFILE" -w '%{http_code}' --connect-timeout 5 --max-time 10 "${BACKEND_URL}/api/health" 2>/dev/null)
    set -e
    HEALTH_CODE="${HEALTH_CODE:-000}"
    HEALTH_BODY=$(cat "$HEALTH_TMPFILE" 2>/dev/null || true)
    if [ "$HEALTH_CODE" = "200" ]; then
        break
    fi
    echo "  attempt ${attempt}: HTTP ${HEALTH_CODE} — retrying in 2s..."
    sleep 2
done
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

# ── Story 2.5 regression: forged Tyk headers must NOT bypass auth ──
header "Story 2.5: forged gateway headers don't bypass standalone auth"
_curl_status() {
    set +e
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "$@" 2>/dev/null)
    set -e
    echo "${code:-000}"
}

NOAUTH_CODE=$(_curl_status "${BACKEND_URL}/api/me")
if [ "$NOAUTH_CODE" = "401" ]; then
    pass "Missing Authorization header → 401"
else
    fail "Missing Authorization header → HTTP ${NOAUTH_CODE} (expected 401)"
fi

# nosemgrep: generic.secrets.security.detected-jwt-token.detected-jwt-token
INVALID_TOKEN="invalid.token.here"
INVALID_CODE=$(_curl_status -H "Authorization: Bearer ${INVALID_TOKEN}" "${BACKEND_URL}/api/me")
if [ "$INVALID_CODE" = "401" ]; then
    pass "Invalid Bearer credential → 401"
else
    fail "Invalid Bearer credential → HTTP ${INVALID_CODE} (expected 401)"
fi

FORGED_CODE=$(_curl_status -H "X-Tyk-Request-ID: forged-request-id-12345" "${BACKEND_URL}/api/me")
if [ "$FORGED_CODE" = "401" ]; then
    pass "Forged X-Tyk-Request-ID without auth → 401 (header does not bypass middleware)"
else
    fail "Forged X-Tyk-Request-ID without auth → HTTP ${FORGED_CODE} (expected 401)"
fi

FORGED_INVALID_CODE=$(_curl_status \
    -H "X-Tyk-Request-ID: forged-request-id-12345" \
    -H "X-Forwarded-For: 10.0.0.1" \
    -H "X-Forwarded-Proto: https" \
    -H "Authorization: Bearer ${INVALID_TOKEN}" \
    "${BACKEND_URL}/api/me")
if [ "$FORGED_INVALID_CODE" = "401" ]; then
    pass "Forged gateway headers + invalid credential → 401"
else
    fail "Forged gateway headers + invalid credential → HTTP ${FORGED_INVALID_CODE} (expected 401)"
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
