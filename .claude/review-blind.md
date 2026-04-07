## Review: Blind Hunter

### MUST FIX

- [`backend/app/services/inbound_sync.py:329`] Wrong operation label on create/link publish — uses `operation="sync"` for the create-or-link branch, but the correct operation for a newly-created user should be `"create"` (which IS used at line 319 for the existing-user update path). The variable `created` is available and distinguishes the two cases; consumers relying on `"create"` will miss cache invalidation for newly created users flowing through this path.

- [`backend/app/services/inbound_sync.py:349`] Wrong operation label on user deletion — `user.deleted` webhook deactivates a user but publishes `operation="sync"` instead of `operation="deactivate"`. The schema documents `"deactivate"` as a valid operation. Any consumer keyed on `"deactivate"` will never receive this event and will not invalidate caches for deactivated users.

- [`backend/uv.lock` (~line 1636, redis package entry)`] `redis` lock entry lists `pyjwt` as a dependency — `redis` has no dependency on `pyjwt`. This is a corrupt lockfile entry, likely a hand-edit or merge artifact. In production it is only harmless if `pyjwt` remains in the dependency tree via another package. If `pyjwt` is removed, `uv` would fail to install the environment. The lockfile must be regenerated with `uv lock` to produce a valid, reproducible entry.

- [`backend/app/main.py:95-103`] Leaked Redis connection when ping fails after TCP connect — on ping failure the `except Exception` block sets `redis_client = None` and discards the reference, but the `aioredis` client object that was allocated at line 98 is never closed. If the connection was established before the ping exception (e.g., AUTH failure, NOAUTH, wrong database), the underlying socket is abandoned without calling `aclose()`. Add `await redis_client.aclose()` in the except block before nulling the reference.

### SHOULD FIX

- [`backend/app/services/cache_invalidation.py:267-275`] `get_cache_publisher()` allocates a fresh `CacheInvalidationPublisher()` instance every time it is called before `init_cache_publisher` runs. Any caller that caches the returned instance before lifespan completes will hold a no-op publisher forever, even after the real one is initialised. A single module-level no-op sentinel (`_NOOP_PUBLISHER = CacheInvalidationPublisher()`) returned from the fallback branch would be safer and eliminates repeated allocation.

- [`backend/app/services/role.py:489`] `update_role` publishes without `tenant_id` — the call has no `tenant_id` kwarg so it defaults to `None`. `create_role` (line 477) and `assign_role_to_user` (line 499) correctly pass `tenant_id`. Roles can be tenant-scoped; consumers filtering on `tenant_id` will miss cache invalidation for tenant-scoped role updates.

- [`backend/app/services/user.py:653-656`] Remove-user-from-tenant publishes `operation="update"` — removing a tenant membership is a removal/unassignment, and the documented schema lists `"unassign"` as a valid operation. `RoleService.unassign_role_from_user` correctly uses `"unassign"`. Using `"update"` here creates inconsistency; consumers cannot distinguish membership removal from a field edit.

- [`backend/app/main.py:115-120`] `shutdown_cache_publisher()` and `redis_client.aclose()` share a single `try/except` block. Although `shutdown_cache_publisher()` currently cannot raise (it is a `global _publisher = None` assignment), if it ever does, `redis_client.aclose()` will be skipped because the except clause absorbs the exception and continues. The two operations should be in separate try/finally blocks to guarantee both execute.

- [`backend/app/services/cache_invalidation.py:230-243`] `publish_batch` constructs the event dict inline and duplicates the `datetime.now(timezone.utc).isoformat()` call that exists in `_build_event`. The batch event diverges from the standard schema independently. If the timestamp format or timezone handling is ever changed in `_build_event`, the batch path will silently produce inconsistent timestamps. Extract a shared `_now_iso()` helper or use `_build_event` for the common fields.

- [`backend/tests/unit/test_cache_invalidation.py:802-819`] `TestSingletonLifecycle` tests mutate module-level global state without using fixtures to guarantee cleanup. If `test_init_and_get_returns_same_instance` fails before reaching its `shutdown_cache_publisher()` cleanup call, all subsequent tests in the file inherit a dirty singleton. Use `setup_method`/`teardown_method` or a pytest fixture with `autouse=True` to guarantee the singleton is reset regardless of test outcome.

- [`backend/app/services/cache_invalidation.py:280-282`] `init_cache_publisher` performs a bare `global _publisher` assignment with no guard against double-initialisation. If called twice (e.g., in tests that forget to call `shutdown_cache_publisher` between runs), the first publisher's Redis client reference is silently replaced without being closed. The old client leaks. Add a warning log or raise if `_publisher` is already set to a non-None value.

### NITPICK

- [`backend/app/main.py:74`] `import os` added solely for `os.getenv("REDIS_URL")`. The project presumably has a settings/config object that validates env vars at startup. Using raw `os.getenv` bypasses any such validation layer and is inconsistent with how the rest of the app likely reads configuration.

- [`backend/app/services/cache_invalidation.py:203`] Constructor parameter is named `redis_client` but the stored attribute is `self._redis` — the shortened name is slightly inconsistent and marginally harder to search for.

- [`backend/app/services/cache_invalidation.py:192`] `CHANNEL = "identity:changes"` is exported implicitly. If downstream consumers import this constant, they are coupling to the internal module path. Worth noting in the module docstring that `CHANNEL` is part of the stable contract.

- [`backend/app/services/inbound_sync.py:305`] `publisher: CacheInvalidationPublisher | None = None` default is consistent with all other services in this PR, but the `if self._publisher:` guard scattered through business logic is noise. A null-object pattern (always inject a real or no-op publisher, never `None`) would eliminate all seven guard clauses across the six service files.
