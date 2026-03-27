task_id: T97
issue:
repo: descope-saas-starter
branch: feat/structured-logging
pr: 57
description: Fix PR #57 review findings — Structured Logging (health check info leak, stale degraded cache, race condition, import-time env vars)
phase: fix

## Findings

### MUST FIX

**[M1] Health check error messages leak internal infrastructure details to unauthenticated callers**
- File: `backend/app/routers/health.py:34,54`
- Issue: Exception class names (e.g., `OperationalError`, `ConnectTimeout`) returned in response body. Reveals DB driver and HTTP library.
- Fix: Return generic "error"/"unavailable". Keep details in server-side logs only.

**[M2] Health check cache serves stale degraded results — prevents recovery detection**
- File: `backend/app/routers/health.py:60-83`
- Issue: Degraded response cached for 30s. If dependency recovers in 1s, pod stays out of rotation for 29s more.
- Fix: Only cache healthy results, or use shorter TTL (5s) for degraded.

**[M3] Race condition in health check cache under concurrent requests**
- File: `backend/app/routers/health.py:60-83`
- Issue: Module-level `_cache` dict with no lock. Concurrent requests all run checks, thundering herd.
- Fix: Use `asyncio.Lock` to serialize cache access.

**[M4] `DESCOPE_PROJECT_ID` and `DESCOPE_BASE_URL` read at import time**
- File: `backend/app/routers/health.py:17-18`
- Issue: If imported before env vars set, health check silently skips Descope and reports "ok".
- Fix: Read inside `_check_descope()` at call time.

### SHOULD FIX

**[S1]** `correlation_id_var.get("-")` redundant default — already set on ContextVar
**[S2]** `_check_descope` creates new `httpx.AsyncClient` per invocation
**[S3]** No `LOG_LEVEL` validation — invalid value crashes at startup
**[S4]** `root.handlers.clear()` nukes all handlers including pytest's
**[S5]** `BaseHTTPMiddleware` known issues with streaming responses
**[S6]** Health check dependency checks run sequentially, not concurrently
**[S7]** Frontend `useRef` flags prevent re-fetching after re-authentication
