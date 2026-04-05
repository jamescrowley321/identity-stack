import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies.identity import get_identity_service
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.identity import IdentityService

router = APIRouter(tags=["Roles"])


class RoleAssignmentRequest(BaseModel):
    user_id: str
    tenant_id: str
    role_names: list[str] = Field(min_length=1)


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    permission_names: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list, max_length=100)


class UpdateRoleRequest(BaseModel):
    new_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    permission_names: list[Annotated[str, Field(min_length=1)]] | None = Field(default=None, max_length=100)


def _parse_uuid(value: str, name: str) -> uuid.UUID:
    """Parse a string to UUID at the router boundary."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {name} format")


# --- Existing user-facing endpoints (must stay before /roles/{name}) ---


@router.get("/roles/me")
async def get_my_roles(
    tenant_id: str = Depends(get_tenant_id),
    tenant_claims: dict = Depends(get_tenant_claims),
):
    """Get the current user's roles and permissions in their active tenant."""
    tenant_info = tenant_claims.get(tenant_id, {})
    if not isinstance(tenant_info, dict):
        tenant_info = {}
    return {
        "tenant_id": tenant_id,
        "roles": tenant_info.get("roles", []),
        "permissions": tenant_info.get("permissions", []),
    }


@router.post("/roles/assign")
@limiter.limit(RATE_LIMIT_AUTH)
async def assign_roles(
    request: Request,
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Assign roles to a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    if "owner" in body.role_names and "owner" not in admin_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")

    tid = _parse_uuid(body.tenant_id, "tenant_id")
    uid = _parse_uuid(body.user_id, "user_id")

    # Pre-validate all role names before mutating state
    resolved_roles: list[uuid.UUID] = []
    for role_name in body.role_names:
        role_result = await service.get_role_by_name(name=role_name)
        if role_result.is_error():
            return result_to_response(role_result, request)
        resolved_roles.append(_parse_uuid(role_result.ok["id"], "role_id"))

    for role_id in resolved_roles:
        assign_result = await service.assign_role_to_user(tenant_id=tid, user_id=uid, role_id=role_id)
        if assign_result.is_error():
            return result_to_response(assign_result, request)

    return {"status": "roles_assigned", "user_id": body.user_id, "role_names": body.role_names}


@router.post("/roles/remove")
@limiter.limit(RATE_LIMIT_AUTH)
async def remove_roles(
    request: Request,
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Remove roles from a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    if "owner" in body.role_names and "owner" not in admin_roles:
        raise HTTPException(status_code=403, detail="Only owners can remove the owner role")

    tid = _parse_uuid(body.tenant_id, "tenant_id")
    uid = _parse_uuid(body.user_id, "user_id")

    # Pre-validate all role names before mutating state
    resolved_roles: list[uuid.UUID] = []
    for role_name in body.role_names:
        role_result = await service.get_role_by_name(name=role_name)
        if role_result.is_error():
            return result_to_response(role_result, request)
        resolved_roles.append(_parse_uuid(role_result.ok["id"], "role_id"))

    for role_id in resolved_roles:
        remove_result = await service.remove_role_from_user(tenant_id=tid, user_id=uid, role_id=role_id)
        if remove_result.is_error():
            return result_to_response(remove_result, request)

    return {"status": "roles_removed", "user_id": body.user_id, "role_names": body.role_names}


# --- Role definition CRUD (admin-only) ---


@router.get("/roles")
async def list_roles(
    request: Request,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """List all role definitions. Requires owner or admin role."""
    result = await service.list_roles()
    if result.is_error():
        return result_to_response(result, request)
    return {"roles": result.ok}


@router.post("/roles", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_role(
    request: Request,
    body: CreateRoleRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Create a new role definition. Requires owner or admin role."""
    # Pre-validate all permission names before mutating state
    resolved_perms: list[uuid.UUID] = []
    for perm_name in body.permission_names:
        perm_result = await service.get_permission_by_name(name=perm_name)
        if perm_result.is_error():
            return result_to_response(perm_result, request)
        resolved_perms.append(_parse_uuid(perm_result.ok["id"], "permission_id"))

    create_result = await service.create_role(name=body.name, description=body.description)
    if create_result.is_error():
        return result_to_response(create_result, request)
    role_dict = create_result.ok
    role_id = _parse_uuid(role_dict["id"], "role_id")

    # Map pre-validated permissions to role
    for perm_id in resolved_perms:
        map_result = await service.map_permission_to_role(role_id=role_id, permission_id=perm_id)
        if map_result.is_error():
            return result_to_response(map_result, request)

    return result_to_response(create_result, request, status=201)


@router.put("/roles/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_role(
    request: Request,
    name: str,
    body: UpdateRoleRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Update a role definition. Requires owner or admin role."""
    role_result = await service.get_role_by_name(name=name)
    if role_result.is_error():
        return result_to_response(role_result, request)
    role_id = _parse_uuid(role_result.ok["id"], "role_id")

    update_result = await service.update_role(
        role_id=role_id,
        name=body.new_name,
        description=body.description,
    )
    return result_to_response(update_result, request)


@router.delete("/roles/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_role(
    request: Request,
    name: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Delete a role definition. Requires owner or admin role."""
    role_result = await service.get_role_by_name(name=name)
    if role_result.is_error():
        return result_to_response(role_result, request)
    role_id = _parse_uuid(role_result.ok["id"], "role_id")

    delete_result = await service.delete_role(role_id=role_id)
    if delete_result.is_error():
        return result_to_response(delete_result, request)
    return {"status": "deleted", "name": name}
