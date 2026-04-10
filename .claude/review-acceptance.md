## Review: Acceptance Auditor

### PASS

- [AC-3.3.2] Redis unavailability — all exceptions in `publish()` and `publish_batch()` are caught with a bare `except Exception`, logged as warnings, and never re-raised. The `repo.commit()` call precedes every `publish()` call in every service, so the Postgres write is always committed before Redis is attempted. A `None` redis client (set when `REDIS_URL` is absent or Redis connection fails at startup) causes `publish()` to return immediately without any network attempt. App startup at `backend/app/main.py:95-106` gracefully handles connection failure by setting `redis_client=None` before calling `init_cache_publisher`. Canonical writes are structurally unaffected.
  - Implemented at `backend/app/services/cache_invalidation.py:62-68` (None guard + except), `backend/app/main.py:92-106` (startup degradation)
  - Tested at `backend/tests/unit/test_cache_invalidation.py:56` (no-op when client is None), `backend/tests/unit/test_cache_invalidation.py:62` (exception swallowed, warning logged, no raise), `backend/tests/unit/test_cache_invalidation.py:111` (batch swallows exception)

- [AC-3.3.3] Subscriber documentation — the module-level docstring at `backend/app/services/cache_invalidation.py:1-24` documents: the channel name (`identity:changes`), the full JSON event schema with allowed values for each field, and key patterns subscribers should use to identify affected records and scope invalidation to a tenant. The schema is validated by tests that deserialize actual published payloads and assert all fields are present with correct values.
  - Implemented at `backend/app/services/cache_invalidation.py:9-23`
  - Tested at `backend/tests/unit/test_cache_invalidation.py:32-54` (channel name, all schema fields including timestamp and null tenant_id verified against a live publish call)

### FAIL

- None

### PARTIAL

- [AC-3.3.1] Publish on write — `CacheInvalidationPublisher.publish()` is called after `repo.commit()` in all four canonical entity services (user, role, permission, tenant) and for all write operations. The event includes all required fields (entity_type, entity_id, operation, tenant_id, timestamp). Channel is `identity:changes`.
  - Implemented at: `backend/app/services/user.py:83,153,200,243,284`; `backend/app/services/role.py:101,168,232,299,332,368`; `backend/app/services/permission.py:74,140,174`; `backend/app/services/tenant.py:73`
  - **What's done:** Implementation is present and correct in all four services. `test_user_service.py:TestCacheInvalidationPublishing` (lines 478-532) covers UserService create + deactivate publish assertions, and a guard test confirms no publish on failure. `test_cache_invalidation.py:TestPublish` validates the full event schema for the `publish()` method itself.
  - **What's missing:** `backend/tests/unit/test_role_service.py`, `backend/tests/unit/test_permission_service.py`, and `backend/tests/unit/test_tenant_service.py` contain zero references to `CacheInvalidationPublisher` or `publisher`. There are no tests that would fail if the `if self._publisher: await self._publisher.publish(...)` blocks were removed from `role.py`, `permission.py`, or `tenant.py`. Three of four canonical entity types lack service-level publish tests.

### SCOPE CREEP

- `backend/app/services/inbound_sync.py:318-319`, `:329`, `:339`, `:349` — `InboundSyncService` publishes cache events for flow-sync and webhook-triggered user mutations (operation `"sync"`). AC-3.3.1 specifies "canonical write operations (create, update, delete on user/role/permission/tenant)." Inbound sync is a secondary write path not listed in the AC's entity/operation enumeration. The publisher is wired via `backend/app/dependencies/identity.py:56`. Not traceable to any AC.

- `backend/app/services/reconciliation.py:156-157` — `ReconciliationService.run()` calls `publish_batch()` after a reconciliation pass with changes. Reconciliation is not a canonical write operation per the spec. Additionally, `publish_batch()` emits events with `entity_type="batch"` and `entity_id="reconciliation"`, which fall outside the documented schema in AC-3.3.3 (entity_type is specified as `user | role | permission | tenant`). Wired via `backend/app/dependencies/identity.py:57`. Not traceable to any AC.

### Architecture Violations

- None identified. `CacheInvalidationPublisher` is injected into domain services via constructor, consistent with D4 (constructor injection, onion architecture). The publisher is always called after `await self._repository.commit()`, preserving the D7 write-through Postgres-first semantic. No publisher calls occur in the repository layer (inner) or adapter layer (outer). The singleton pattern mirrors the existing `descope.py` module pattern. The Redis import is gated behind `TYPE_CHECKING` to avoid a hard load-time import.

### Summary

- Total ACs: 3
- Pass: 2 | Fail: 0 | Partial: 1

The partial is AC-3.3.1: implementation is complete and correct across all four canonical entity services, but three of the four service test files (role, permission, tenant) have no publisher assertions. Removing the publish calls from those three services would not be caught by the test suite.
