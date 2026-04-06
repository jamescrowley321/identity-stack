## Review: Acceptance Auditor

### PASS

- [AC-2.2.1] Role repository + service — `RoleRepository` at `backend/app/repositories/role.py` handles all SQLAlchemy queries for role CRUD and role-permission mappings (`create`, `get`, `get_by_name`, `list_by_tenant`, `update`, `add_permission`, `remove_permission`, `get_permissions`, `commit`). `RoleService` at `backend/app/services/role.py` orchestrates role CRUD via `RoleRepository` and syncs via `DescopeSyncAdapter.sync_role()`. `create_role()` accepts `name`, `description`, and optional `tenant_id`, persists a `Role` with `tenant_id=NULL` for global roles, calls `adapter.sync_role()`, and returns `Result[dict, IdentityError]`. Tested at `backend/tests/unit/test_role_service.py:77` (success path), `test_role_service.py:91` (with tenant), `test_role_service.py:103` (commit-before-sync order).

- [AC-2.2.2] Permission repository + service — `PermissionRepository` at `backend/app/repositories/permission.py` handles all SQLAlchemy queries for permission CRUD. `PermissionService.create_permission()` at `backend/app/services/permission.py:42` persists via repository and syncs to Descope. `map_permission_to_role()` is implemented on `RoleService` at `backend/app/services/role.py:107`, creates a `RolePermission` mapping via `RoleRepository.add_permission()` and syncs. Note: the AC places this method under the PermissionService AC but the spec text does not mandate which service owns it; `RoleService` ownership is architecturally coherent since it controls both sides of the mapping. Tested at `backend/tests/unit/test_permission_service.py:48` (create), `test_role_service.py:181` (map, including conflict and sync payload).

- [AC-2.2.3] Tenant repository + service — `TenantRepository` at `backend/app/repositories/tenant.py` handles all SQLAlchemy queries for tenant CRUD. `TenantService.create_tenant()` at `backend/app/services/tenant.py:42` accepts `name` and `domains`, creates a `Tenant` via repository, and syncs via `adapter.sync_tenant()`. `get_tenant_users_with_roles()` at `backend/app/services/tenant.py:97` verifies tenant existence, then calls `TenantRepository.get_users_with_roles()` which executes a 3-way JOIN across `users`, `user_tenant_roles`, and `roles`. Tested at `backend/tests/unit/test_tenant_service.py:49` (create), `test_tenant_service.py:151` (users with roles grouped by user), `test_tenant_service.py:184` (multiple users), `test_tenant_service.py:214` (tenant not found).

- [AC-2.2.4] User-tenant-role assignment — `assign_role_to_user()` at `backend/app/services/role.py:157` accepts `user_id`, `tenant_id`, `role_id`, `assigned_by`; creates a `UserTenantRole` record via `UserTenantRoleRepository.create()`; `UserTenantRole` model at `backend/app/models/identity/assignment.py:29` includes `assigned_by` and `assigned_at` fields; sync is attempted via `adapter.sync_role_assignment()`; when the same assignment already exists (pre-checked via `.get()` and caught on `RepositoryConflictError`), `Error(Conflict(...))` is returned. Tested at `backend/tests/unit/test_role_service.py:257` (success with `assigned_by`), `test_role_service.py:286` (existing assignment returns Conflict), `test_role_service.py:298` (TOCTOU race returns Conflict), `test_role_service.py:310` (sync failure still returns Ok).

- [AC-2.2.5] Duplicate constraints — `RoleService.create_role()` returns `Error(Conflict(...))` on duplicate name within same tenant scope, both via pre-check and TOCTOU race. `PermissionService.create_permission()` returns `Error(Conflict(...))` on duplicate name. Tested at `backend/tests/unit/test_role_service.py:118` (role duplicate via pre-check), `test_role_service.py:129` (role TOCTOU race), `test_permission_service.py:77` (permission duplicate), `test_permission_service.py:88` (permission TOCTOU race).

- [AC-2.2.6] Onion layer compliance — Repositories contain no OTel spans (verified: no `opentelemetry` imports in role.py, permission.py, tenant.py, assignment.py repositories), no adapter calls, no business logic — data access only. Services contain no direct SQLAlchemy imports (verified: zero `from sqlalchemy`/`import sqlalchemy` lines in `role.py`, `permission.py`, `tenant.py` services). DI factories at `backend/app/dependencies/identity.py:47,68,82` compose `session -> repository -> service(repository, adapter)` for all three services. Tested at `backend/tests/unit/test_identity_dependency.py:90` (RoleService: 3 repos all share same session instance), `test_identity_dependency.py:119` (PermissionService), `test_identity_dependency.py:144` (TenantService).

### FAIL

- [AC-2.2.6 / Enforcement Guideline 7] Repository tests must use real Postgres via testcontainers — **no integration tests exist for any of the four new repositories** (`RoleRepository`, `PermissionRepository`, `TenantRepository`, `UserTenantRoleRepository`). The `db_session` testcontainers fixture is defined at `backend/tests/integration/conftest.py:152` and `noop_adapter` at `conftest.py:180`, but neither is consumed by any test. The integration directory contains only `test_api.py` and `test_session.py`, neither exercising any new repository. Guideline 7 is explicit: "Repository tests use real Postgres via testcontainers — never mock the database." All four repositories are validated only through service-layer mocks, which cannot verify SQL correctness, JOIN behavior, constraint enforcement, or PostgreSQL-specific indexes (e.g., the partial unique index `ix_roles_name_global` on `roles(name) WHERE tenant_id IS NULL`).

### PARTIAL

_(none)_

### SCOPE CREEP

_(none — the `sync_role_assignment` implementation upgrade in `backend/app/services/adapters/descope.py` is directly required by AC-2.2.4's "sync to Descope is attempted via adapter" clause, which previously had only a placeholder)_

### Architecture Violations

- [Guideline 7] All four new repositories lack integration tests against real Postgres. The `db_session` + `noop_adapter` testcontainers fixtures exist in the integration `conftest.py` but are unused. SQL query logic, JOIN correctness, unique-constraint handling, and PostgreSQL-specific partial index behavior (`ix_roles_name_global`) are entirely unvalidated by tests.

- [Guideline 6 — pre-existing, not introduced by this diff] `backend/tests/unit/test_tenant_router.py:29-32` uses `SQLModel.metadata.create_all` on an in-memory SQLite engine instead of Alembic migrations. PostgreSQL-specific DDL (e.g., partial indexes) will silently not apply on SQLite, meaning router tests exercise a divergent schema. This pattern predates this story and is not introduced by this diff.

### Summary

- Total ACs: 6 (AC-2.2.1 through AC-2.2.6)
- Pass: 5 | Fail: 1 | Partial: 0

**The single failure is critical:** all four new repositories have no integration tests against a real Postgres instance. The service layer, DI wiring, adapter implementation, and duplicate-constraint handling are all correctly implemented and tested at the unit level. But Guideline 7 is a hard requirement, and the absence of any Postgres-backed repository tests means SQL-level correctness (JOINs, constraints, partial indexes) is entirely unverified.
