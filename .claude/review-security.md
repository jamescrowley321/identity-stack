## Review: Security (Sentinel)

### BLOCK (must fix before merge)

None.

---

### WARN (should fix)

- [LIKELY] `backend/app/services/adapters/descope.py:171` — `sync_role_assignment` passes `role_id` (a canonical UUID) as the `roleNames` value to Descope's Management API, but Descope's `/v1/mgmt/user/update/role/add` endpoint expects role *names* (strings like `"admin"`), not internal UUIDs.

  The docstring acknowledges this: *"Requires data dict with role_name for the Descope API call. Falls back to using role_id as string if role_name not provided."* The implementation only does the fallback — it never actually receives or uses a role name because the `sync_role_assignment` interface takes no `data` parameter.

  **Impact:** Every `assign_role_to_user` call will silently fail at the Descope sync step (the call will 4xx because a UUID is not a valid Descope role name). The canonical DB is updated, but Descope is never updated correctly. Since sync failures are swallowed and `Ok(...)` is returned, this creates a persistent divergence: the canonical store says the user has the role; Descope does not; JWT tokens issued by Descope will not carry the expected role claims, breaking `require_role()` enforcement for newly assigned roles.

  **Mitigation:** Add a `data: dict` parameter to `sync_role_assignment` matching the pattern used by `sync_role`, `sync_permission`, and `sync_tenant`. Pass `{"role_name": role.name}` from `RoleService.assign_role_to_user` after fetching the role object (which is already fetched at line 805 of the diff). The existing `assign_roles` router endpoint in `roles.py` correctly passes role names — the adapter should mirror that contract.

---

### INFO (acceptable risk)

- `backend/app/services/role.py`, `permission.py`, `tenant.py` — Sync failures are intentionally swallowed (log at WARNING, return `Ok`). Acceptable architectural decision for write-through with tolerated divergence, provided the sync path sends correct data. See WARN above for the one case where it does not.

- `backend/app/repositories/assignment.py:140-155` — `list_by_user_tenant` requires both `user_id` and `tenant_id` from the service layer. No IDOR vector: callers always supply tenant scope from validated JWT context.

- `backend/app/repositories/tenant.py:447-460` — `get_users_with_roles` filters by explicit `tenant_id` parameter in the WHERE clause; no cross-tenant data leak.

- `backend/app/services/adapters/descope.py:171` — Also passes `str(user_id)` (canonical UUID) as `loginId`, whereas Descope mutations require the `loginId` (email/phone). This compounds the WARN above but does not add an independent security impact beyond the sync divergence already described.

- New dependency factories (`get_role_service`, `get_permission_service`, `get_tenant_service`) — Only reachable through the middleware-protected router stack via FastAPI `Depends(get_async_session)`. No bypass vector introduced. No new HTTP endpoints are added in this diff.

---

### Summary

- BLOCK: 0 | WARN: 1 | INFO: 4
- Overall: **PASS** (with fix recommended)

The WARN produces a security-relevant divergence (canonical RBAC and JWT-issuing IdP out of sync), but since Descope is the authoritative token issuer and `require_role()` enforces against live JWT claims, a user will not gain elevated access — they will lose access that was granted. No privilege escalation path. Fix before the sync path is relied upon in production.
