## Review: Security (Sentinel)

### BLOCK (must fix before merge)

- none

The prior review's BLOCK (unauthenticated flow sync endpoint) has been fully remediated. The flow sync endpoint at `backend/app/routers/internal.py:98` now requires an `X-Flow-Secret` header validated via `verify_flow_sync_secret` dependency (line 56-69) using `hmac.compare_digest()` for timing-safe comparison against `DESCOPE_FLOW_SYNC_SECRET`. Missing or invalid secrets return 401. The webhook endpoint retains HMAC-SHA256 validation. Both endpoints are protected.

### WARN (should fix)

- **W1** [`backend/app/routers/internal.py:50`] `WebhookPayload.data` is an unvalidated `dict` with no schema enforcement. Webhook handlers extract fields via `.get()` with empty-string defaults (`inbound_sync.py:188-189,205,223,226-228,258`). A malformed payload that is missing expected fields silently returns `Ok({"status": "skipped"})` rather than an error, making production debugging difficult. If Descope changes its payload shape, the endpoint silently stops processing events with no alerting. Consider adding per-event-type Pydantic models or at minimum a structured warning log when required fields are absent for known event types.

- **W2** [`backend/app/routers/internal.py:36-50`] No input length constraints on request model fields. `user_id` is a bare `str` with no `max_length`. `name`, `given_name`, `family_name` are similarly unconstrained. `WebhookPayload.data` is an unbounded `dict`. An attacker with the shared secret (or a compromised Descope instance) could submit arbitrarily large payloads that consume memory or fill database columns. The database columns use `sa.String` without length limits (`backend/app/models/identity/user.py:28-31`). Add `max_length` constraints on Pydantic fields (e.g., `user_id: str = Field(max_length=256)`) and consider a body size limit.

- **W3** [`backend/app/middleware/factory.py:79-80`] Gateway mode has no layered protection for internal endpoints. In gateway mode, `TokenValidationMiddleware` is not registered at all, so the `excluded_prefixes` mechanism is irrelevant. The flow sync shared secret and webhook HMAC still apply, which is correct. However, the comment at line 42-43 states "Network-level isolation or a shared gateway secret ensures only Tyk can reach the backend directly" -- until that network isolation is implemented, internal endpoints in gateway mode rely solely on the shared secrets with no additional defense. This should be documented as a deployment prerequisite and validated in gateway mode startup.

- **W4** [`backend/app/services/inbound_sync.py:110-112,151-153,245-247,284-286`] Commit failures return `Conflict` error type regardless of cause. The bare `except Exception` blocks around `commit()` catch all exceptions (including network errors, connection timeouts, etc.) and map them to `Conflict` error responses. A database connectivity failure should not be presented to callers as a conflict. This could mask operational issues and mislead debugging. Consider distinguishing between constraint violations and infrastructure failures.

- **W5** [`backend/app/services/inbound_sync.py:223-225`] The `_handle_user_updated` webhook path reads `email` from the untyped webhook `data` dict and writes it directly to the user model without email format validation (unlike the flow sync path which uses Pydantic `EmailStr`). A webhook event with a malformed email value (e.g., empty string that passes the truthy check, or a string without `@`) would be persisted to the database. While SQLAlchemy parameterizes all values (no injection risk), this creates data quality degradation that could break downstream email-dependent logic. Consider validating webhook email values before assignment.

### INFO (acceptable risk)

- **I1** PII handling in OTel traces is properly mitigated. The source code at `inbound_sync.py:27-29,66` uses `_hash_email(email)` (SHA-256 truncated to 12 hex chars) for the `user.email_hash` span attribute rather than raw email. The `descope.user_id` (an external IdP identifier, not PII by itself) is set as a span attribute, which is acceptable.

- **I2** PII in log messages is properly handled. Logger calls at `inbound_sync.py:114,156,249,288` include only `user.id` (a UUID, not PII). Skip-reason logs at lines `192,207-208,259-260` log `list(data.keys())` rather than raw data values, avoiding PII leakage from webhook payloads.

- **I3** HMAC implementation is cryptographically correct. `verify_hmac_signature` (`internal.py:75-92`) uses `hmac.new()` with SHA-256 and `hmac.compare_digest()` for timing-safe comparison. The secret is read once at import time (not per-request from env). Missing header returns 422 before HMAC logic executes.

- **I4** Flow sync shared secret implementation is correct. `verify_flow_sync_secret` (`internal.py:56-69`) uses `hmac.compare_digest()` for timing-safe string comparison. Empty/missing secret configuration is rejected at the dependency level with 401, and startup logs a warning (`main.py:41-42`).

- **I5** Rate limiting is applied to both internal endpoints. Flow sync uses `@limiter.limit(RATE_LIMIT_AUTH)` at line 99 and webhook at line 123 (default: 10/minute). In standalone mode, SlowAPI middleware is active. The rate limit key falls back to client IP for unauthenticated requests.

- **I6** `excluded_prefixes` implementation is not vulnerable to path traversal. `backend/app/middleware/auth.py:43` uses `str.startswith(tuple)`. Paths like `/api/internal/../protected` are normalized by ASGI servers before reaching the middleware.

- **I7** Fail-closed behavior on missing secrets. When `DESCOPE_WEBHOOK_SECRET` is empty/unset, the webhook endpoint rejects all requests with 401 (`internal.py:84-86`). Same for `DESCOPE_FLOW_SYNC_SECRET` (`internal.py:64-66`). Startup warnings at `main.py:39-42` alert operators.

- **I8** No SQL injection risk. All data flows through SQLAlchemy ORM parameterized queries. User-supplied strings from request models and webhook data are used as ORM attribute assignments or `where()` clause parameters, both properly parameterized.

- **I9** Secrets loaded at import time (`internal.py:29-30`) are not reloadable without restart. Acceptable for current deployment model but worth noting for secret rotation procedures.

- **I10** IdPLinkRepository no longer calls `rollback()` on shared session. The actual source (`idp_link.py:42-47`) raises `RepositoryConflictError` on `IntegrityError` without rolling back, letting the service layer control transaction boundaries. This is correct.

### Summary
- BLOCK: 0 | WARN: 5 | INFO: 10
- Overall: PASS
