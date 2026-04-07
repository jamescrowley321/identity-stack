## Review: Acceptance Auditor

### PASS
- [AC-3.1.1a] POST /api/internal/users/sync endpoint exists -- implemented at `backend/app/routers/internal.py:75`, tested at `backend/tests/unit/test_internal_router.py:91` (201 for new), `backend/tests/unit/test_internal_router.py:106` (200 for update)
- [AC-3.1.1b] Canonical User record created in Postgres -- implemented at `backend/app/services/inbound_sync.py:110-124` (creates User via UserRepository), tested at `backend/tests/unit/test_inbound_sync_service.py:814` (`test_new_user_created`)
- [AC-3.1.1c] IdP link created (provider: descope, external_sub: descope_user_id) -- implemented at `backend/app/services/inbound_sync.py:127-136`, tested at `backend/tests/unit/test_inbound_sync_service.py:814` (asserts `link_repo.create.assert_awaited_once()`)
- [AC-3.1.1d] If user already exists by email, existing record updated -- implemented at `backend/app/services/inbound_sync.py:84-104` (existing link path) and `backend/app/services/inbound_sync.py:106-107` (existing user by email path), tested at `backend/tests/unit/test_inbound_sync_service.py:835` (`test_existing_link_updates_user`) and `backend/tests/unit/test_inbound_sync_service.py:858` (`test_existing_user_by_email_creates_link`)
- [AC-3.1.1e] 201 for new user, 200 for update -- implemented at `backend/app/routers/internal.py:94` (status logic based on `created` flag), tested at `backend/tests/unit/test_internal_router.py:91-103` (201) and `backend/tests/unit/test_internal_router.py:106-114` (200)
- [AC-3.1.3a] HMAC-SHA256 validation using DESCOPE_WEBHOOK_SECRET -- implemented at `backend/app/routers/internal.py:51-69` (verify_hmac_signature dependency), tested at `backend/tests/unit/test_internal_router.py:182-203` (`test_webhook_valid_hmac`)
- [AC-3.1.3b] Invalid HMAC returns 401 -- implemented at `backend/app/routers/internal.py:68-69`, tested at `backend/tests/unit/test_internal_router.py:209-219` (`test_webhook_invalid_hmac_returns_401`) and `backend/tests/e2e/test_internal_endpoints_e2e.py:62-72`
- [AC-3.1.3c] Missing/empty secret returns 401 -- implemented at `backend/app/routers/internal.py:61-63`, tested at `backend/tests/unit/test_internal_router.py:233-243` (`test_webhook_missing_secret_returns_401`)
- [AC-3.1.4a] Internal endpoints bypass JWT auth -- implemented at `backend/app/middleware/factory.py:74-76` (excluded_prefixes `/api/internal/`) and `backend/app/middleware/auth.py:43` (prefix check in dispatch), tested at `backend/tests/unit/test_internal_router.py:73-84` (`test_flow_sync_no_auth_required`), `backend/tests/unit/test_internal_router.py:246-257` (`test_webhook_no_auth_required`), and `backend/tests/e2e/test_internal_endpoints_e2e.py:27-56`

### FAIL
- (none)

### PARTIAL
- [AC-3.1.2a] Webhook handler processes Descope audit events -- The handler_map at `backend/app/services/inbound_sync.py:157-161` implements `user.created`, `user.updated`, and `user.deleted`. The spec's Technical Notes list six event types: `user.created`, `user.updated`, `user.deleted`, `role.created`, `role.updated`, `permission.modified`. The latter three are not explicitly handled -- they fall through to the "unknown event type" path (logged as warning, returns success). This is a reasonable design choice (graceful degradation), and the user-related events do correctly update the canonical store (user.created syncs user+link at `inbound_sync.py:170-185`, user.updated modifies fields at `inbound_sync.py:187-229`, user.deleted deactivates at `inbound_sync.py:231-259`). Tests cover all three user event types plus unknown event handling at `backend/tests/unit/test_inbound_sync_service.py:1016-1161`. The role/permission event types are not tested because they are not implemented.
- [AC-3.1.2b] Idempotent processing (event ID) -- The spec's Technical Notes state: "Idempotency key: use the event ID from Descope to prevent duplicate processing." The implementation does NOT use event IDs. The `WebhookPayload` model at `backend/app/routers/internal.py:41-45` has no `event_id` field, and no event ID storage/deduplication table exists. Instead, idempotency is achieved structurally: `sync_user_from_flow` checks for an existing IdP link by `(provider_id, external_sub)` at `backend/app/services/inbound_sync.py:82`, and replayed `user.updated`/`user.deleted` events are inherently idempotent (re-applying the same update or re-deactivating an already inactive user). This achieves the functional goal -- replayed events do not create duplicates -- as tested at `backend/tests/unit/test_inbound_sync_service.py:835` (re-sync updates rather than duplicates), but it does not implement the specified mechanism (event ID deduplication).

### SCOPE CREEP
- (none detected -- all new code is traceable to AC-3.1.x requirements)

### Architecture Violations
- (none detected)
  - Guideline 1 (AsyncSession only): All repositories (`IdPLinkRepository`, `ProviderRepository`) use `AsyncSession` via constructor injection. PASS.
  - Guideline 2 (Result[T, IdentityError]): Both `sync_user_from_flow` and `process_webhook_event` return `Result[dict, IdentityError]`. PASS.
  - Guideline 3 (result_to_response): Router endpoints use `result_to_response()` at `internal.py:95` and `internal.py:113`. PASS.
  - Guideline 4 (OTel spans with domain attributes on every domain service method): Spans with domain attributes on `sync_user_from_flow` (`inbound_sync.py:58-60`, attributes: `descope.user_id`, `user.email`) and `process_webhook_event` (`inbound_sync.py:154-155`, attribute: `webhook.event_type`). Private helper methods called within those spans do not add their own spans, which is correct. PASS.
  - Guideline 5 (Repositories: no business logic, no OTel, no adapters): `IdPLinkRepository` and `ProviderRepository` are pure data access -- no spans, no business logic, no adapter calls. PASS.
  - Guideline 6 (Domain services: no direct SQLAlchemy imports): `InboundSyncService` has zero `sqlalchemy` imports -- uses repository abstractions only. PASS.
  - Guideline 7 (Follow existing router patterns): Internal router uses `APIRouter`, `Depends`, `result_to_response`, Pydantic request models, and `Request` injection consistent with `users.py`, `roles.py`, etc. PASS.
  - Anti-pattern check (no HTTPException in service): No `raise HTTPException` in `InboundSyncService`. The `raise HTTPException` calls at `internal.py:63,69` are in the HMAC validation dependency (router layer), consistent with existing patterns in `users.py:28,58`, `tenants.py:28,35,87`, etc. PASS.

### Summary
- Total ACs: 11 (decomposed from 4 spec ACs)
- Pass: 9 | Fail: 0 | Partial: 2

The two partial items are:
1. **Missing role/permission event handlers**: `role.created`, `role.updated`, `permission.modified` event types from the spec's Technical Notes are not explicitly handled. They degrade gracefully (logged warning, success response) but do not update the canonical store for those entity types.
2. **Event ID idempotency not implemented**: The spec prescribes event ID-based deduplication. The implementation uses structural idempotency (IdP link uniqueness, idempotent updates) instead. The functional outcome is equivalent -- replayed events do not create duplicates -- but the mechanism differs from what was specified.

Both are reasonable engineering trade-offs. The core user-facing behavior (flow sync, webhook processing, HMAC validation, JWT bypass) is correctly implemented and thoroughly tested at both unit and E2E levels.
