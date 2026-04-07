## Review: Security (Sentinel)

### BLOCK (must fix before merge)

- none

### WARN (should fix)

**From prior review (Story 3.1 — still open):**

- **W1** [`backend/app/routers/internal.py:50`] `WebhookPayload.data` is an unvalidated `dict` with no schema enforcement. Webhook handlers extract fields via `.get()` with empty-string defaults. A malformed payload silently returns `Ok({"status": "skipped"})` rather than an error, making production debugging difficult. If Descope changes its payload shape, the endpoint silently stops processing events with no alerting. Consider adding per-event-type Pydantic models or at minimum structured warning logs when required fields are absent for known event types.

- **W2** [`backend/app/routers/internal.py:36-50`] No input length constraints on request model fields. `user_id` is a bare `str` with no `max_length`. `name`, `given_name`, `family_name` are similarly unconstrained. `WebhookPayload.data` is an unbounded `dict`. An attacker with the shared secret could submit arbitrarily large payloads. Add `max_length` constraints on Pydantic fields and consider a body size limit.

- **W3** [`backend/app/middleware/factory.py:79-80`] Gateway mode has no layered protection for internal endpoints. Until network-level isolation is implemented, internal endpoints in gateway mode rely solely on shared secrets with no additional defense. This should be documented as a deployment prerequisite.

- **W4** [`backend/app/services/inbound_sync.py:110-112,151-153`] Commit failures return `Conflict` error type regardless of cause. Bare `except Exception` blocks around `commit()` map all exceptions (including network errors, connection timeouts) to `Conflict` responses. A database connectivity failure should not be presented as a conflict.

- **W5** [`backend/app/services/inbound_sync.py:223-225`] The `_handle_user_updated` webhook path reads `email` from the untyped webhook `data` dict and writes it directly to the user model without email format validation (unlike the flow sync path which uses Pydantic `EmailStr`). A malformed email value would be persisted to the database.

**New findings — Story 3.3 (cache invalidation):**

- **W6** [`backend/app/main.py:93-98`] Redis URL is consumed without scheme validation. `aioredis.from_url(redis_url)` accepts any URL scheme the `redis` library supports, including `unix://`. If `REDIS_URL` is misconfigured or injected (e.g., via a compromised secrets manager), the application will silently attempt to connect to a non-Redis endpoint. The graceful-degradation catch block swallows the error, which is intentional, but a badly formed URL pointing at an internal metadata endpoint (e.g., `http://169.254.169.254/...`) would cause a connection attempt to that host at startup without any scheme allowlist.
  Mitigation: Validate `redis_url` scheme before calling `aioredis.from_url()` — e.g., assert scheme in `{"redis", "rediss"}`.

- **W7** [`backend/app/services/cache_invalidation.py:267-275`] `get_cache_publisher()` has a singleton gap: if called before `init_cache_publisher()` (e.g., in tests that import the module without going through app lifespan), it returns a throwaway no-op `CacheInvalidationPublisher()` that is not stored in `_publisher`. A consumer that caches this returned reference before init completes will hold a permanently no-op publisher and silently miss all cache invalidation events after Redis connects. The security implication is that downstream caches may serve stale identity or permission data indefinitely with no indication of the misconfiguration.
  Mitigation: Document init ordering requirement; add a warning log if `get_cache_publisher()` is called before `init_cache_publisher()`.

### INFO (acceptable risk)

- **I1** PII handling in OTel traces is properly mitigated. `inbound_sync.py` uses `_hash_email(email)` (SHA-256 truncated) for span attributes rather than raw email. UUIDs in span attributes are not PII.

- **I2** PII in log messages is properly handled. Logger calls include only `user.id` (a UUID). Skip-reason logs emit `list(data.keys())` rather than raw data values, avoiding PII leakage from webhook payloads.

- **I3** HMAC implementation is cryptographically correct. `verify_hmac_signature` uses `hmac.new()` with SHA-256 and `hmac.compare_digest()` for timing-safe comparison. Secret read once at import time.

- **I4** Flow sync shared secret implementation is correct. `verify_flow_sync_secret` uses `hmac.compare_digest()` for timing-safe string comparison. Empty/missing secret configuration is rejected with 401.

- **I5** Rate limiting is applied to both internal endpoints (default: 10/minute via `RATE_LIMIT_AUTH`). SlowAPI middleware is active in standalone mode.

- **I6** `excluded_prefixes` implementation is not vulnerable to path traversal. Uses `str.startswith(tuple)`; ASGI servers normalize paths before middleware.

- **I7** Fail-closed behavior on missing secrets. When `DESCOPE_WEBHOOK_SECRET` or `DESCOPE_FLOW_SYNC_SECRET` is empty/unset, the respective endpoints reject all requests with 401.

- **I8** No SQL injection risk. All data flows through SQLAlchemy ORM parameterized queries.

- **I9** Secrets loaded at import time (`internal.py:29-30`) are not reloadable without restart. Acceptable for current deployment model.

- **I10** Cache invalidation events contain no credentials or PII. Published events carry only UUIDs and enum-like strings. The `stats` dict in `publish_batch` contains only server-side integer counters — no user-supplied data flows into the Redis channel payload. A compromised Redis subscriber learns only that a mutation occurred on a given entity type/ID, not the content of the change.

- **I11** Redis auth is configured in docker-compose. `REDIS_URL` includes password authentication; Redis port 6379 is bound to `127.0.0.1` only, limiting exposure to the Docker network. The default `changeme` password is documented as overridable via `REDIS_PASSWORD`.

- **I12** Cache invalidation failure is genuinely fire-and-forget. All `publish()` and `publish_batch()` calls are wrapped in `try/except Exception`. Failures never propagate to the service layer and never roll back committed DB transactions. A Redis outage cannot cause data inconsistency in the canonical store.

- **I13** No SSRF risk from Redis URL under normal operation. The URL is taken from a server-side environment variable, not from any user-supplied request parameter. (W6 above covers the env-injection edge case.)

- **I14** `publish_batch` stats integrity. The `stats` dict is initialized with hardcoded string keys and integer zero values; only `+= 1` increments modify it before publication. No user-controlled data reaches the published stats payload.

- **I15** `shutdown_cache_publisher()` correctly nulls the singleton before Redis client is closed (`main.py:87-89`). Subsequent calls to `get_cache_publisher()` after shutdown return a new no-op instance, preventing use-after-close of the Redis client.

### Summary
- BLOCK: 0 | WARN: 7 | INFO: 15
- Overall: PASS
