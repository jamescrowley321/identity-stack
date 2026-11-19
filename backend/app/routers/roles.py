import uuid
from typing import Annotated

from expression import Error, Ok
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies.identity import get_role_service
from app.dependencies.rbac import require_global_role, require_role
from app.dependencies.sync_provider import require_sync_provider_principal
from app.dependencies.tenant import get_tenant_access, get_tenant_id
from app.dependencies.tenant_access import TenantAccess
from app.errors.identity import NotFound
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.role import RoleService

# ``RoleService`` pushes each definition and each assignment to the configured sync
# adapter (``SYNC_PROVIDER``), which takes the request's tenant and user ids as that
# provider's: every route but ``/roles/me`` serves its principals only (conventions
# §18). The definitions belong to no tenant, so their routes are gated by
# ``require_global_role``: an admin of one of several canonical tenants cannot name
# that tenant to change them for every tenant. ``/roles/me`` writes nothing and
# answers any principal the tenant resolver places.
router = APIRouter(tags=["Roles"])
SYNC_PROVIDER_ONLY = [Depends(require_sync_provider_principal)]


class RoleAssignmentRequest(BaseModel):
    user_id: str
    tenant_id: str
    role_names: list[str] = Field(min_length=1)


class CreateRoleRequest(BaseModel):
    # Descope accepts up to 255 chars; standardized across all routers
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=1000)
    # Up to 100 permissions per role — generous but bounded
    permission_names: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list, max_length=100)


class UpdateRoleRequest(BaseModel):
    # Descope accepts up to 255 chars; standardized across all routers
    new_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    # Up to 100 permissions per role — generous but bounded
    permission_names: list[Annotated[str, Field(min_length=1)]] | None = Field(default=None, max_length=100)


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    """Parse a string to UUID, raising 422 on invalid input."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid UUID for {field_name}: {value}")


# --- Existing user-facing endpoints (must stay before /roles/{name}) ---


@router.get("/roles/me")
async def get_my_roles(access: TenantAccess = Depends(get_tenant_access)):
    """The caller's roles and permissions in the request's tenant, as the tenant resolver grants them."""
    return {"tenant_id": access.tenant_id, "roles": access.roles, "permissions": access.permissions}


@router.post("/roles/assign", dependencies=SYNC_PROVIDER_ONLY)
@limiter.limit(RATE_LIMIT_AUTH)
async def assign_roles(
    request: Request,
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    admin_roles: list[str] = Depends(require_role("owner", "admin")),
    role_service: RoleService = Depends(get_role_service),
):
    """Assign roles to a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    if "owner" in body.role_names and "owner" not in admin_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")

    tenant_uuid = _parse_uuid(body.tenant_id, "tenant_id")
    user_uuid = _parse_uuid(body.user_id, "user_id")

    # Resolve role names to canonical IDs
    roles_result = await role_service.list_roles()
    if roles_result.is_error():
        return result_to_response(roles_result, request)
    role_map = {r["name"]: r["id"] for r in roles_result.ok}

    for role_name in body.role_names:
        role_id = role_map.get(role_name)
        if role_id is None:
            return result_to_response(Error(NotFound(message=f"Role '{role_name}' not found")), request)
        result = await role_service.assign_role_to_user(
            user_id=user_uuid,
            tenant_id=tenant_uuid,
            role_id=uuid.UUID(str(role_id)),
        )
        if result.is_error():
            return result_to_response(result, request)

    return result_to_response(
        Ok({"status": "roles_assigned", "user_id": body.user_id, "role_names": body.role_names}),
        request,
    )


@router.post("/roles/remove", dependencies=SYNC_PROVIDER_ONLY)
@limiter.limit(RATE_LIMIT_AUTH)
async def remove_roles(
    request: Request,
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    admin_roles: list[str] = Depends(require_role("owner", "admin")),
    role_service: RoleService = Depends(get_role_service),
):
    """Remove roles from a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    if "owner" in body.role_names and "owner" not in admin_roles:
        raise HTTPException(status_code=403, detail="Only owners can remove the owner role")

    tenant_uuid = _parse_uuid(body.tenant_id, "tenant_id")
    user_uuid = _parse_uuid(body.user_id, "user_id")

    # Resolve role names to canonical IDs
    roles_result = await role_service.list_roles()
    if roles_result.is_error():
        return result_to_response(roles_result, request)
    role_map = {r["name"]: r["id"] for r in roles_result.ok}

    for role_name in body.role_names:
        role_id = role_map.get(role_name)
        if role_id is None:
            return result_to_response(Error(NotFound(message=f"Role '{role_name}' not found")), request)
        result = await role_service.unassign_role_from_user(
            user_id=user_uuid,
            tenant_id=tenant_uuid,
            role_id=uuid.UUID(str(role_id)),
        )
        if result.is_error():
            return result_to_response(result, request)

    return result_to_response(
        Ok({"status": "roles_removed", "user_id": body.user_id, "role_names": body.role_names}),
        request,
    )


# --- Role definition CRUD (admin-only) ---


@router.get("/roles", dependencies=SYNC_PROVIDER_ONLY)
async def list_roles(
    request: Request,
    _admin_roles: list[str] = Depends(require_global_role("owner", "admin")),
    role_service: RoleService = Depends(get_role_service),
):
    """List all role definitions. Requires owner or admin role."""
    result = await role_service.list_roles()
    if result.is_ok():
        result = Ok({"roles": result.ok})
    return result_to_response(result, request)


@router.post("/roles", status_code=201, dependencies=SYNC_PROVIDER_ONLY)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_role(
    request: Request,
    body: CreateRoleRequest,
    _admin_roles: list[str] = Depends(require_global_role("owner", "admin")),
    role_service: RoleService = Depends(get_role_service),
):
    """Create a new role definition. Requires owner or admin role."""
    result = await role_service.create_role(
        name=body.name,
        description=body.description,
        permission_names=body.permission_names or None,
    )
    return result_to_response(result, request, status=201)


@router.put("/roles/{name}", dependencies=SYNC_PROVIDER_ONLY)
@limiter.limit(RATE_LIMIT_AUTH)
async def update_role(
    request: Request,
    name: str,
    body: UpdateRoleRequest,
    _admin_roles: list[str] = Depends(require_global_role("owner", "admin")),
    role_service: RoleService = Depends(get_role_service),
):
    """Update a role definition. Requires owner or admin role."""
    # Resolve role name to canonical ID
    roles_result = await role_service.list_roles()
    if roles_result.is_error():
        return result_to_response(roles_result, request)
    role_match = next((r for r in roles_result.ok if r["name"] == name), None)
    if role_match is None:
        return result_to_response(Error(NotFound(message=f"Role '{name}' not found")), request)

    result = await role_service.update_role(
        role_id=uuid.UUID(str(role_match["id"])),
        name=body.new_name,
        description=body.description,
        permission_names=body.permission_names,
    )
    return result_to_response(result, request)


@router.delete("/roles/{name}", dependencies=SYNC_PROVIDER_ONLY)
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_role(
    request: Request,
    name: str,
    _admin_roles: list[str] = Depends(require_global_role("owner", "admin")),
    role_service: RoleService = Depends(get_role_service),
):
    """Delete a role definition. Requires owner or admin role."""
    # Resolve role name to canonical ID
    roles_result = await role_service.list_roles()
    if roles_result.is_error():
        return result_to_response(roles_result, request)
    role_match = next((r for r in roles_result.ok if r["name"] == name), None)
    if role_match is None:
        return result_to_response(Error(NotFound(message=f"Role '{name}' not found")), request)

    result = await role_service.delete_role(role_id=uuid.UUID(str(role_match["id"])))
    return result_to_response(result, request)
