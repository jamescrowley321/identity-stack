task_id: T96
issue:
repo: descope-saas-starter
branch: feat/rate-limiting
base_branch: feat/admin-portal
pr: 56
description: Fix PR #56 review findings — Rate Limiting (proxy IP keying, middleware ordering, hardcoded Retry-After)
phase: complete

## Findings

### MUST FIX

**[M1] Rate limit key for unauthenticated requests uses `request.client.host` — wrong behind proxy**
- File: `backend/app/middleware/rate_limit.py:22`
- Issue: Behind load balancer, every request comes from proxy IP. All unauthenticated users share one bucket. Single attacker locks out all unauthenticated traffic.
- Fix: Use `ProxyHeadersMiddleware` to set `request.client` from trusted proxy headers.

**[M2] Middleware ordering — rate limits on protected endpoints fire AFTER auth, not before**
- File: `backend/app/main.py:46-47`
- Issue: `SlowAPIMiddleware` is a pass-through; actual checks happen at decorator level inside route handlers, which execute after `TokenValidationMiddleware`. Unauthenticated brute-force gets 401 before rate limit fires.
- Fix: Fix misleading comments. Verify `/validate-id-token` (excluded from auth) is effectively limited.

**[M3] Hardcoded `Retry-After: 60` doesn't reflect actual retry window**
- File: `backend/app/middleware/rate_limit.py:31`
- Issue: Always says 60 regardless of actual window or reset time.
- Fix: Compute from rate limit reset time.

### SHOULD FIX

**[S1]** `_inject_headers` is private API — fragile across slowapi updates
**[S2]** `get_rate_limit_key` bare `except Exception: pass` swallows all errors
**[S3]** Missing rate limiting on destructive admin endpoints (roles, users, tenants)
**[S4]** `validate_id_token` returns 200 with error body instead of proper HTTP error
**[S5]** Rate limit env var format not validated at startup
**[S6]** Test uses `get_remote_address` instead of actual `get_rate_limit_key`
**[S7]** No rate limiting on unauthenticated `/validate-id-token` replay attacks
