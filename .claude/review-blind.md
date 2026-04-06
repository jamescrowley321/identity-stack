## Review: Blind Hunter

### MUST FIX

- [`backend/app/services/adapters/descope.py:490`] `assign_roles` is called with `str(role_id)` (the canonical UUID) instead of the role's actual Descope name — Descope's `assign_roles` API expects role names (strings like `"admin"`), not UUIDs. The adapter docstring even acknowledges "Falls back to using role_id as string if role_name not provided." A UUID is never a valid Descope role name, so every call will silently fail at the Descope API level (or raise, which is caught and returned as `Error`). The role_name must be resolved before this call; there is no mechanism in the adapter signature to pass it in.

- [`backend/app/repositories/assignment.py:136`] `await self._session.rollback()` is called inside `create()` after an `IntegrityError` during `flush()`. The session is shared across all three repositories wired in `get_role_service()`. Rolling back here silently invalidates any pending work in `RoleRepository` or `PermissionRepository` that shares the same `AsyncSession`, without the caller knowing. A repository calling `rollback()` on a shared session is catastrophic: the service layer that owns the transaction will commit or rollback independently, and one of those two operations will be on an already-rolled-back session, causing an `InvalidRequestError`.

- [`backend/app/repositories/permission.py:206`] Same shared-session rollback problem as `assignment.py:136`. `PermissionRepository.create()` calls `await self._session.rollback()` on the injected session. All three repositories in `RoleService` share one `AsyncSession` (proven by the wiring test at line 1114-1116). Rolling back in one repository tears down the whole unit of work silently.

- [`backend/app/repositories/role.py:287`] Same shared-session rollback problem. `RoleRepository.create()` calls `await self._session.rollback()`. Additionally, `RoleRepository.add_permission()` at line 339 does the same. The service catches `RepositoryConflictError` and returns `Error`, but by that point the session has been rolled back, so any subsequent `commit()` in the service operates on an invalid transaction state.

- [`backend/app/repositories/tenant.py:415`] Same shared-session rollback problem in `TenantRepository.create()`.

### SHOULD FIX

- [`backend/app/services/role.py:769-784`] `repo.get_permissions(role_id)` is called *after* `repo.commit()` at line 769. The commit closes the current transaction. Accessing a new query in post-commit state can raise `DetachedInstanceError` or open an implicit new transaction depending on session configuration. The permission names fetch should happen before the commit, not after.

- [`backend/app/services/permission.py:583-585`] `result_dict = permission.model_dump()` is captured before `commit()`. If `commit()` raises an exception (e.g. a constraint violation caught at commit time rather than flush time), the exception propagates unhandled as a raw `SQLAlchemyError` rather than a typed `Result` error. Same pattern in `role.py` line 709 and `tenant.py` lines 929-931.

- [`backend/app/services/role.py:838-841`] `_log_sync_failure` is passed `role_id` as the `entity_id` for a role assignment sync failure. The log message reads "IdP sync failed for role %s" but this operation concerns a user-tenant-role assignment. Operators diagnosing failures will not be able to identify which user is affected.

- [`backend/app/repositories/role.py:349`] `remove_permission` calls `await self._session.flush()` unconditionally after the DELETE but does not handle `IntegrityError`. If a foreign key constraint fires on delete, the raw `SQLAlchemyError` propagates uncaught through the service layer. Every other mutating method in the repository wraps the flush in a try/except.

- [`backend/app/repositories/assignment.py`] `UserTenantRoleRepository` has no `delete` method. If a role assignment needs to be revoked, there is no data-access path. `remove_permission` exists on `RoleRepository` for the analogous operation but the equivalent is absent here.

- [`backend/app/services/tenant.py:980`] `get_tenant_users_with_roles` iterates `rows` assuming each element unpacks as `(user, role)`. If the `sa.select()` column ordering in the repository ever changes, the unpacking silently swaps fields and corrupts the response. The return type hint `list[tuple]` does not encode the ordering contract.

### NITPICK

- [`backend/app/services/adapters/descope.py:493`] Sync failure is logged at `DEBUG` level. Every other sync failure path in the service layer logs at `WARNING`. A failed sync to an external identity provider is operationally significant and will be invisible at default production log levels.

- [`backend/app/repositories/assignment.py:140`] The `get()` method signature places all three parameters on a single long line, exceeding PEP 8 line length. Minor style issue with no functional impact.
