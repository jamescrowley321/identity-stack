## Review: Red Team (Viper)

### Attack Surface

1. **POST /api/internal/users/sync** -- New endpoint for Descope Flow HTTP Connector user synchronisation. Accepts `user_id`, `email`, `name`, `given_name`, `family_name`. Protected by shared secret (`X-Flow-Secret` header validated against `DESCOPE_FLOW_SYNC_SECRET` via `verify_flow_sync_secret` dependency at `internal.py:98`). JWT auth bypassed via `excluded_prefixes`. Rate-limited at `RATE_LIMIT_AUTH` (default 10/minute).
2. **POST /api/internal/webhooks/descope** -- New endpoint for Descope audit webhook events. Accepts `event_type` and arbitrary `data` dict. Protected by HMAC-SHA256 (`X-Descope-Webhook-S256` header validated against `DESCOPE_WEBHOOK_SECRET`). JWT auth bypassed. Rate-limited at `RATE_LIMIT_AUTH`.
3. **JWT bypass via excluded_prefixes** -- Entire `/api/internal/` prefix excluded from `TokenValidationMiddleware` in `factory.py:74-76`. Any future endpoint registered under this prefix automatically inherits the bypass.
4. **Shared secret authentication on flow sync** -- `verify_flow_sync_secret` dependency at `internal.py:56-69` performs timing-safe comparison of `X-Flow-Secret` header against `_FLOW_SYNC_SECRET` module-level variable.
5. **HMAC-SHA256 authentication on webhook** -- `verify_hmac_signature` dependency at `internal.py:75-92` computes HMAC over raw request body and compares against `X-Descope-Webhook-S256` header.
6. **InboundSyncService** -- New service layer at `inbound_sync.py` that creates/updates User records and IdPLink records in Postgres based on external input.
7. **IdPLinkRepository** -- New repository at `idp_link.py` for IdP link CRUD, with unique constraints on `(provider_id, external_sub)` and `(user_id, provider_id)`.
8. **ProviderRepository** -- New repository at `provider.py` for provider lookup by type/name.
9. **Startup secret validation** -- `_warn_missing_secrets()` in `main.py:35-42` logs warnings at startup when `DESCOPE_WEBHOOK_SECRET` or `DESCOPE_FLOW_SYNC_SECRET` are not set. Both endpoints fail closed (401) when secrets are missing.

### Findings

#### MEDIUM -- Unbounded string length on user_id and name fields enables storage abuse

- **Location**: `backend/app/routers/internal.py:39-43` (`FlowSyncRequest` model), `backend/app/models/identity/user.py:69` (`external_sub` column), `backend/app/models/identity/user.py:30-31` (`given_name`/`family_name` columns)
- **Attack scenario**:
  1. Attacker obtains the `DESCOPE_FLOW_SYNC_SECRET` shared secret (e.g., leaked in logs, config repo, or insider threat).
  2. Attacker sends POST to `/api/internal/users/sync` with `user_id` set to a multi-megabyte string and valid `X-Flow-Secret` header.
  3. `FlowSyncRequest.user_id` is typed as `str` with no `max_length` constraint. The value flows through to `IdPLink.external_sub` which is `sa.Column(sa.String, nullable=False)` -- unbounded VARCHAR.
  4. Similarly, `name`, `given_name`, `family_name` have no length limits and are stored in unbounded `sa.String` columns.
  5. Repeated requests (up to 10/minute per IP due to rate limiting) with large payloads bloat the database.
- **Prerequisites**: Knowledge of `DESCOPE_FLOW_SYNC_SECRET`. Rate limiting (10/min) constrains throughput.
- **CVSS v3.1**: 4.3 (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:L)
- **Remediation**: Add `max_length` constraints to `FlowSyncRequest` fields: `user_id: str = Field(max_length=255)`, `name: str | None = Field(default=None, max_length=512)`, `given_name: str | None = Field(default=None, max_length=255)`, `family_name: str | None = Field(default=None, max_length=255)`. Also add `String(255)` or similar length to the SQLAlchemy column definitions for `external_sub`, `given_name`, `family_name` in the ORM models.

#### MEDIUM -- WebhookPayload.data typed as unvalidated dict accepts arbitrary nested structures

- **Location**: `backend/app/routers/internal.py:49-50` (`WebhookPayload` model), `backend/app/services/inbound_sync.py:186-289` (webhook handlers)
- **Attack scenario**:
  1. Attacker compromises the HMAC webhook secret.
  2. The `data` field accepts any JSON object with no schema validation per event type.
  3. In `_handle_user_updated` (line 223-236), `data.get("email")` could return a non-string type (e.g., a list or dict). This value is assigned directly to `user.email` at line 225. Depending on SQLAlchemy's type coercion for the `sa.String` column, this could raise an unhandled exception (500 error) or store a stringified representation.
  4. `data.get("name")` could return a non-string. When passed to `name.split(" ", 1)` at line 234, a non-string raises `AttributeError`, which propagates as an unhandled 500.
  5. A deeply nested or very large `data` dict could consume excessive memory during JSON parsing.
- **Prerequisites**: Valid HMAC signature (requires `DESCOPE_WEBHOOK_SECRET`).
- **CVSS v3.1**: 4.3 (AV:N/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:L)
- **Remediation**: Define typed inner models for the `data` field per event type, or validate field types in the service layer before use. At minimum, add type guards: `if isinstance(email, str)` before assignment, `if isinstance(name, str)` before calling `.split()`.

#### LOW -- Module-level secret caching prevents runtime secret rotation

- **Location**: `backend/app/routers/internal.py:29-30`
- **Attack scenario**:
  1. A secret is compromised and the operator rotates `DESCOPE_FLOW_SYNC_SECRET` or `DESCOPE_WEBHOOK_SECRET` in the environment.
  2. The application continues using the old (compromised) secret because `_FLOW_SYNC_SECRET` and `_WEBHOOK_SECRET` are read once at module import time.
  3. The operator must restart the application process for the new secret to take effect.
  4. During the window between rotation and restart, the compromised secret remains valid.
- **Prerequisites**: Secret compromise plus operational delay in restarting. This is a deployment hygiene issue rather than a direct exploit vector.
- **CVSS v3.1**: 2.6 (AV:N/AC:H/PR:H/UI:N/S:U/C:N/I:L/A:N)
- **Remediation**: Acceptable trade-off. The comment at line 28 ("Read secrets once at import time; validated at startup in lifespan") documents the intentional design. If runtime rotation is later desired, read from environment per-request with TTL caching, or integrate a secrets manager.

#### LOW -- Webhook log messages include data.keys() which could contain attacker-controlled strings

- **Location**: `backend/app/services/inbound_sync.py:192,207,260`
- **Attack scenario**:
  1. Attacker sends a webhook with crafted data keys containing malicious strings (e.g., ANSI escape sequences, HTML/JS payloads).
  2. `logger.warning("... keys=%s", list(data.keys()))` writes attacker-controlled key names to application logs.
  3. If logs are rendered in a web-based log viewer without sanitization, the key names could be exploited for log injection.
- **Prerequisites**: Valid HMAC signature plus a vulnerable log viewer rendering the log entries.
- **CVSS v3.1**: 2.0 (AV:N/AC:H/PR:L/UI:R/S:U/C:N/I:L/A:N)
- **Remediation**: Minor concern. The approach of logging only keys (not values) is already good practice. For additional hardening, log only the count of unexpected keys or sanitize key names.

### Non-findings (verified secure)

- **Flow sync authentication**: The flow sync endpoint IS authenticated. `internal.py:98` specifies `dependencies=[Depends(verify_flow_sync_secret)]`, which validates the `X-Flow-Secret` header against `_FLOW_SYNC_SECRET` using `hmac.compare_digest()` (line 68). Missing or incorrect secret returns 401. Tests at `test_internal_router.py:114-146` cover invalid secret, missing header, and unconfigured secret scenarios. PASS.
- **Timing-safe comparison**: Both `verify_flow_sync_secret` (line 68) and `verify_hmac_signature` (line 91) use `hmac.compare_digest()`, preventing timing side-channel attacks on secret/signature comparison. PASS.
- **HMAC construction**: `hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()` at line 89 correctly computes HMAC-SHA256 over the raw request body with the secret as the HMAC key. PASS.
- **Fail-closed on missing secrets**: Both `verify_flow_sync_secret` (lines 64-66) and `verify_hmac_signature` (lines 84-86) reject requests with 401 when the respective secret is empty or unset. Startup warnings in `main.py:39-42` alert operators. PASS.
- **Email PII in OTel spans**: The service uses `_hash_email()` at `inbound_sync.py:27-29` to SHA-256 hash emails before setting span attributes (line 66: `span.set_attribute("user.email_hash", _hash_email(email))`). No raw email addresses in traces. PASS.
- **SQL injection**: All queries use parameterized SQLAlchemy expressions (e.g., `IdPLink.external_sub == external_sub`), never string interpolation. PASS.
- **IDOR on user records**: Users are looked up via IdP link (`provider_id + external_sub`) or by email, never by attacker-supplied canonical UUIDs. User IDs in responses are server-generated. PASS.
- **Rate limiting**: Both endpoints decorated with `@limiter.limit(RATE_LIMIT_AUTH)` (default 10/minute) at `internal.py:99` and `internal.py:123`. The rate limiter falls back to IP address when no JWT claims are present (as expected for internal endpoints). PASS.
- **IdPLink unique constraints**: `uq_idp_links_provider_external_sub` prevents duplicate IdP links for the same provider/subject. `uq_idp_links_user_provider` prevents a user from having multiple links to the same provider. Database-level enforcement. PASS.
- **Repository transaction ownership**: `IdPLinkRepository.create()` at `idp_link.py:42-47` does NOT call `rollback()` on `IntegrityError` -- it raises `RepositoryConflictError` and lets the service layer control the transaction. Comment at line 41 explicitly documents this. PASS.
- **Commit error handling**: The service wraps all `commit()` calls in try/except blocks (e.g., lines 108-112, 149-153 of `inbound_sync.py`), returning typed `Result` errors instead of propagating raw exceptions as 500s. PASS.

### Summary

- Attack surface elements: 9
- Findings: 0 critical, 0 high, 2 medium, 2 low
- Overall: PASS

The implementation has a solid security posture. Both internal endpoints are authenticated (shared secret for flow sync, HMAC-SHA256 for webhooks), use timing-safe comparisons, fail closed when secrets are unconfigured, and are rate-limited. Email PII is hashed before inclusion in OTel spans. Repository transaction ownership is correctly managed. The two medium findings (unbounded string lengths and unvalidated webhook data types) are defense-in-depth improvements that require a pre-existing secret compromise to exploit. No critical or high-severity vulnerabilities were identified.
