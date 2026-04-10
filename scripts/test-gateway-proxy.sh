#!/usr/bin/env bash
#
# Verify Tyk gateway proxy and header forwarding.
# Requires: gateway profile running (docker compose --profile gateway up)
#
# Usage:
#   ./scripts/test-gateway-proxy.sh
#   TYK_URL=http://localhost:8080 ./scripts/test-gateway-proxy.sh
#
set -euo pipefail

# Ensure we run from the repo root (required for relative config file paths)
cd "$(dirname "$0")/.."

TYK_URL="${TYK_URL:-http://localhost:8080}"
PASS=0
FAIL=0
SKIP=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  SKIP: $1"; SKIP=$((SKIP + 1)); }

header() { echo -e "\n=== $1 ==="; }

# ── Pre-flight: check gateway is reachable ──
header "Pre-flight"
# /api/health is auth-exempt (ignored path in Tyk config), so 200 means reachable
if ! curl -sf -o /dev/null --connect-timeout 5 "${TYK_URL}/api/health" 2>/dev/null; then
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 "${TYK_URL}/api/health" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "000" ]; then
        echo "ERROR: Tyk gateway not reachable at ${TYK_URL}"
        echo "Start with: docker compose --profile gateway up"
        exit 1
    fi
    echo "Gateway reachable at ${TYK_URL} (pre-flight got HTTP ${HTTP_CODE})"
else
    echo "Gateway reachable at ${TYK_URL}"
fi

# ── AC1: Tyk proxies GET /api/health to backend (auth-exempt) ──
header "AC1: Health endpoint proxy (no auth required)"
# /api/health is an ignored path in Tyk config — bypasses JWT validation
HEALTH_TMPFILE=$(mktemp)
trap 'rm -f "$HEALTH_TMPFILE"' EXIT
HEALTH_CODE=$(curl -s -w '%{http_code}' -o "$HEALTH_TMPFILE" "${TYK_URL}/api/health" 2>/dev/null || echo "000")
HEALTH_RESPONSE=$(cat "$HEALTH_TMPFILE" 2>/dev/null || true)
rm -f "$HEALTH_TMPFILE"

if [ "$HEALTH_CODE" = "200" ]; then
    if echo "$HEALTH_RESPONSE" | grep -q '"status"'; then
        pass "GET /api/health returns 200 with status field (no auth required)"
    else
        fail "GET /api/health returned 200 but unexpected body: ${HEALTH_RESPONSE}"
    fi
else
    fail "GET /api/health returned HTTP ${HEALTH_CODE} (expected 200 — path should be auth-exempt)"
fi

# ── AC2: Tyk sets proxy headers (X-Forwarded-For, X-Forwarded-Proto, X-Real-IP) ──
# Config-based verification: Tyk automatically sets X-Forwarded-For, X-Forwarded-Proto,
# and X-Real-IP for all proxied requests (built-in gateway behavior). We verify that
# preserve_host_header is enabled in the API definition. Runtime header inspection
# would require a debug/echo endpoint on the backend.
header "AC2: Proxy header forwarding"
# Verify Tyk config has preserve_host_header enabled
if [ -f "tyk/apps/saas-backend.json" ]; then
    if grep -q '"preserve_host_header": true' tyk/apps/saas-backend.json; then
        pass "Tyk API definition has preserve_host_header: true"
    else
        fail "Tyk API definition missing preserve_host_header: true"
    fi
else
    fail "Tyk API definition file not found at tyk/apps/saas-backend.json"
fi

# NOTE: X-Forwarded-For, X-Forwarded-Proto, X-Real-IP are set by Tyk's reverse proxy
# layer automatically for all proxied requests. This is built-in behavior documented at
# https://tyk.io/docs/. Runtime verification would require a backend debug/echo endpoint.
echo "  INFO: X-Forwarded-For, X-Forwarded-Proto, X-Real-IP headers are set by Tyk automatically (built-in behavior, no runtime check available)"

# ── AC3: Authorization header forwarded to backend ──
# Config-based verification: Tyk's strip_auth_data=false ensures the original
# Authorization: Bearer <token> header is forwarded to the backend. Runtime
# verification requires a valid JWT in the test environment.
header "AC3: Authorization header forwarding"
if [ ! -f "tyk/apps/saas-backend.json" ]; then
    fail "Tyk API definition file not found at tyk/apps/saas-backend.json"
elif grep -q '"strip_auth_data": false' tyk/apps/saas-backend.json; then
    pass "strip_auth_data is false — Authorization header forwarded to backend"
else
    fail "strip_auth_data should be false to forward Authorization header"
fi

# ── AC4: Invalid/expired JWT rejected with 401 ──
header "AC4: Invalid JWT rejection"
# Test against /api/me (a protected endpoint) — /api/health is auth-exempt
# nosemgrep: generic.secrets.security.detected-jwt-token.detected-jwt-token
INVALID_JWT="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0IiwiZXhwIjoxMDAwMDAwMDAwfQ.invalid-signature"
INVALID_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${INVALID_JWT}" \
    "${TYK_URL}/api/me" 2>/dev/null || echo "000")

if [ "$INVALID_CODE" = "401" ] || [ "$INVALID_CODE" = "403" ]; then
    pass "Invalid JWT rejected with HTTP ${INVALID_CODE}"
else
    fail "Invalid JWT got HTTP ${INVALID_CODE} (expected 401 or 403)"
fi

# ── AC5: Missing Authorization header rejected with 401 ──
header "AC5: Missing auth rejection"
NOAUTH_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    "${TYK_URL}/api/me" 2>/dev/null || echo "000")

if [ "$NOAUTH_CODE" = "401" ] || [ "$NOAUTH_CODE" = "403" ]; then
    pass "Missing Authorization header rejected with HTTP ${NOAUTH_CODE}"
elif [ "$NOAUTH_CODE" = "200" ]; then
    # /api/health might be exempt, but /api/me should require auth
    fail "Missing Authorization header was not rejected (got 200)"
else
    fail "Missing Authorization header got HTTP ${NOAUTH_CODE} (expected 401)"
fi

# ── Summary ──
header "Results"
echo "  ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "GATEWAY PROXY VERIFICATION FAILED"
    exit 1
else
    echo "GATEWAY PROXY VERIFICATION PASSED"
    exit 0
fi
