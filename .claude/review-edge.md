## Review: Edge Case Hunter

### Findings

| Location | Trigger Condition | Guard Snippet | Consequence |
|----------|-------------------|---------------|-------------|
| `backend/app/services/adapters/descope.py:171` | `sync_role_assignment` sends UUID string as role name to Descope `assign_roles` | `role_name = data.get("role_name", str(role_id)); await self._client.assign_roles(..., [role_name])` | [WRONG] Descope API called with UUID not role name; Descope rejects or silently assigns unknown role; DB is committed but IdP sync is always effectively broken |
| `backend/app/services/role.py:140` | `get_permissions` called after `commit()` in `map_permission_to_role`; if DB raises on the post-commit select, exception propagates unhandled | `try: permissions = await self._repository.get_permissions(role_id) except Exception: permission_names = []` | [CRASH] Unhandled SQLAlchemy exception escapes service layer; FastAPI returns 500 to caller |
| `backend/app/repositories/role.py:99` | `remove_permission` flushes after DELETE; non-IntegrityError DB exception propagates unhandled to caller | `try: await self._session.flush() except Exception as exc: raise RepositoryConflictError(str(exc)) from exc` | [CRASH] Callers of `remove_permission` have no handler for OperationalError/DisconnectionError; unhandled exception escapes to FastAPI returning 500 |

### Summary
- Unhandled paths found: 3
- Critical (crash/data loss): 2
- Non-critical (wrong result/degraded): 1

#### Notes on Findings

**Finding 1 (wrong IdP sync — WRONG):** `DescopeSyncAdapter.sync_role_assignment` (line 171) passes `str(role_id)` — a UUID string — as the sole element of `role_names` to `self._client.assign_roles(...)`. The Descope Management API `assign_roles` endpoint maps `roleNames` to Descope role name strings, not internal UUIDs. The docstring acknowledges this ("Falls back to using role_id as string if role_name not provided"), but no `data` dict parameter is accepted by this method, so there is no code path that can provide a role name. The local DB commit always succeeds first, so the assignment is durable in the canonical store, but the Descope sync will produce a wrong call every time. The `except Exception` catches any Descope rejection and returns `Error(SyncError)`, which `_log_sync_failure` degrades to a warning log — silent from the caller's perspective.

**Finding 2 (post-commit query unguarded — CRASH):** In `RoleService.map_permission_to_role` (line 140), `get_permissions` is called after `commit()` to build the sync payload. If the DB connection is lost between commit and this select, `SQLAlchemyError` (not caught anywhere in the service) propagates to the router. No router wires this service yet in this diff, but once wired, any DB failure at this point will produce an unhandled 500.

**Finding 3 (remove_permission flush — CRASH):** `RoleRepository.remove_permission` (line 99) calls `await self._session.flush()` without any try/except. A DELETE flush can raise `OperationalError` or `DisconnectionError`. No caller in this diff wraps `remove_permission` calls, so those exceptions propagate unhandled. This is a new file, so the gap belongs to this change.
