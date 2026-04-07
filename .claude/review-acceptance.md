## Review: Acceptance Auditor

### PASS

- [AC-3.1.1] POST /api/internal/users/sync endpoint (Flow HTTP Connector) -- Endpoint implemented at `backend/app/routers/internal.py:98` as `POST /internal/users/sync` (mounted with `/api` prefix in `main.py:106`). Request model `FlowSyncRequest` (line 36) validates `user_id: str` and `email: EmailStr` via Pydantic. Service method `InboundSyncService.sync_user_from_flow` at `backend/app/services/inbound_sync.py:50` creates a canonical `User` via `UserRepository.create()` (line 131) and an `IdPLink` via `IdPLinkRepository.create()` (line 145) with `provider_id` from the Descope provider and `external_sub` set to the Descope `user_id`. When an existing IdP link is found (line 90), the linked user is updated instead. When a user exists by email but has no link, a new IdP link is created without duplicating the user (lines 118-157). Router returns 201 for new users (`created=True` in result dict, line 118) and 200 for updates (`created=False`). Tested at `test_internal_router.py:165` (201 for new), `test_internal_router.py:181` (200 for existing), and comprehensively in `test_inbound_sync_service.py:81` (new user creation + link), `test_inbound_sync_service.py:102` (existing link updates user), `test_inbound_sync_service.py:125` (existing user by email creates link only). E2E coverage at `test_internal_endpoints_e2e.py:88-107` (missing field and invalid email validation).

- [AC-3.1.3] HMAC-SHA256 validation on webhook using DESCOPE_WEBHOOK_SECRET -- Implemented at `backend/app/routers/internal.py:75-92` via `verify_hmac_signature` dependency on the webhook route (line 122). Uses `hmac.new()` with SHA256 to compute the expected signature over the raw request body, then validates via `hmac.compare_digest()` for timing-safe comparison. Invalid signature returns 401 (line 92). Missing/empty `DESCOPE_WEBHOOK_SECRET` returns 401 with "Webhook secret not configured" (line 86). Secret is read at module import time from the env var (line 30), with a startup warning logged when absent (`main.py:39`). Tested at `test_internal_router.py:262` (valid HMAC processed successfully), `test_internal_router.py:294` (invalid HMAC returns 401), `test_internal_router.py:323` (missing secret config returns 401), `test_internal_router.py:312` (missing header returns 422 from FastAPI). E2E coverage at `test_internal_endpoints_e2e.py:66` (invalid HMAC returns 401), `test_internal_endpoints_e2e.py:78` (missing header returns 422).

- [AC-3.1.4] Internal endpoints bypass JWT auth (/api/internal/ prefix excluded) -- Configured at `backend/app/middleware/factory.py:74-76` where `excluded_prefixes={"/api/internal/"}` is passed to `TokenValidationMiddleware`. The middleware at `backend/app/middleware/auth.py:43` checks `request.url.path.startswith(self.excluded_prefixes)` and calls `call_next` without JWT validation for matching paths. Both internal endpoints are therefore exempt from JWT auth. The flow sync endpoint is instead protected by a shared secret (`verify_flow_sync_secret` at `internal.py:56-69`); the webhook endpoint is protected by HMAC (AC-3.1.3). Tested at `test_internal_router.py:78` (flow sync works without Authorization header, gets 201 not 401), `test_internal_router.py:339` (webhook without JWT gets 422 from missing HMAC header, not 401 from JWT). E2E at `test_internal_endpoints_e2e.py:30` (flow sync returns 422 for missing flow secret, not JWT 401), `test_internal_endpoints_e2e.py:49` (webhook returns 422 for missing HMAC, not JWT 401).

### FAIL

- (none)

### PARTIAL

- [AC-3.1.2] POST /api/internal/webhooks/descope webhook handler -- The endpoint exists at `backend/app/routers/internal.py:122` and routes to `InboundSyncService.process_webhook_event` at `backend/app/services/inbound_sync.py:159`. Two sub-issues:
  - **(a) Missing event types:** The handler_map at lines 173-177 implements `user.created`, `user.updated`, and `user.deleted`. The AC specifies six event types: `user.created`, `user.updated`, `user.deleted`, `role.created`, `role.updated`, `permission.modified`. The latter three are not explicitly handled -- they fall through to the "unknown event type" path (logged as warning at line 181, returns `Ok({"status": "ignored"})`). This is graceful degradation rather than failure, and the three user event types are correctly implemented: `user.created` delegates to `sync_user_from_flow` (line 195), `user.updated` modifies fields on the linked user (lines 203-250), `user.deleted` deactivates the user via `status=inactive` (lines 252-289). Tests cover all three user event types plus unknown event handling at `test_inbound_sync_service.py:283-428`.
  - **(b) Idempotency mechanism:** The AC states "Idempotent processing." The spec's Technical Notes further state: "Idempotency key: use the event ID from Descope to prevent duplicate processing." The implementation does NOT use event IDs -- `WebhookPayload` at `internal.py:46-50` has no `event_id` field, and no event deduplication table exists. Instead, idempotency is achieved structurally: `sync_user_from_flow` checks for existing IdP link by `(provider_id, external_sub)` at `inbound_sync.py:88`; replayed `user.updated` events re-apply the same field values; replayed `user.deleted` events re-deactivate an already inactive user. The functional outcome is correct -- replayed events do not create duplicates -- but the mechanism differs from the specified event ID deduplication approach. Tested at `test_inbound_sync_service.py:102` (re-sync updates rather than duplicates).

### SCOPE CREEP

- Flow sync shared secret authentication (`verify_flow_sync_secret` at `internal.py:56-69`, using `DESCOPE_FLOW_SYNC_SECRET` env var with timing-safe comparison) -- The ACs only specify HMAC for the webhook endpoint (AC-3.1.3) and JWT bypass for internal endpoints (AC-3.1.4). Adding shared-secret auth on the flow sync endpoint is a sensible security measure not traceable to a specific AC. Low risk, defensible addition.
- Startup secret warnings (`_warn_missing_secrets()` at `main.py:35-42`) -- Logs warnings at startup when `DESCOPE_WEBHOOK_SECRET` or `DESCOPE_FLOW_SYNC_SECRET` are not set. Operationally helpful, not required by any AC.

### Architecture Violations

- (none detected)
  - **Guideline 1 (AsyncSession only):** `IdPLinkRepository` (`idp_link.py:25`) and `ProviderRepository` (`provider.py:24`) accept `AsyncSession` via constructor injection. PASS.
  - **Guideline 2 (Result[T, IdentityError]):** Both `sync_user_from_flow` and `process_webhook_event` return `Result[dict, IdentityError]`. All private handler methods return the same type. PASS.
  - **Guideline 3 (result_to_response):** Router uses `result_to_response()` at `internal.py:119` (flow sync with dynamic status) and `internal.py:138` (webhook). PASS.
  - **Guideline 4 (OTel spans on domain service methods):** `sync_user_from_flow` has span at `inbound_sync.py:64` with attributes `descope.user_id` and `user.email_hash` (email is hashed via `_hash_email()`, not raw PII). `process_webhook_event` has span at `inbound_sync.py:170` with attribute `webhook.event_type`. Private helpers execute within the parent span and correctly do not add redundant child spans. PASS.
  - **Guideline 5 (Repositories: no business logic, no OTel, no adapters):** `IdPLinkRepository` and `ProviderRepository` contain zero `opentelemetry` imports, no adapter calls, no business logic -- pure data access only. Verified via grep. PASS.
  - **Guideline 6 (Domain services: no direct SQLAlchemy imports):** `InboundSyncService` has zero `sqlalchemy` imports -- uses repository method abstractions exclusively. Verified via grep. PASS.
  - **Guideline 7 (Follow existing router patterns):** Internal router uses `APIRouter`, `Depends()`, `result_to_response()`, Pydantic request models (`FlowSyncRequest`, `WebhookPayload`), and `Request` injection -- consistent with `users.py`, `roles.py`, `tenants.py`. PASS.
  - **Anti-pattern check (no HTTPException in service):** Zero `HTTPException` references in `InboundSyncService`. Verified via grep. The `raise HTTPException` calls at `internal.py:66,69,86,92` are in router-layer auth dependencies, consistent with existing patterns in other routers. PASS.
  - **DI wiring:** `get_inbound_sync_service` at `dependencies/identity.py:95-110` correctly wires `AsyncSession -> UserRepository + IdPLinkRepository + ProviderRepository -> InboundSyncService`. All three repositories share the same session instance, maintaining the unit-of-work pattern. PASS.

### Summary

- Total ACs: 4 (AC-3.1.1, AC-3.1.2, AC-3.1.3, AC-3.1.4)
- Pass: 3 | Fail: 0 | Partial: 1

The single partial item is **AC-3.1.2** (webhook handler), with two sub-issues: (1) only 3 of 6 specified event types are implemented (`role.created`, `role.updated`, `permission.modified` are missing but degrade gracefully), and (2) event ID-based idempotency deduplication is not implemented as specified, though structural idempotency achieves the same functional outcome for the implemented event types.

The two scope creep items (flow sync shared secret, startup warnings) are both defensible security/operational additions that do not introduce risk and are low in complexity.

All architecture guidelines are satisfied. The onion architecture layering is clean: repositories handle data access only, the domain service contains business logic with no SQLAlchemy or HTTPException leakage, the router uses Depends/result_to_response/Pydantic models, and OTel spans are correctly placed on public service methods with appropriate attributes.
