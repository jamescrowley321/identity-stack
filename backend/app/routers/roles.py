import uuid
from typing import Annotated

from expression import Error, Ok
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies.identity import get_role_service
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.errors.identity import NotFound
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.role import RoleService

router = APIRouter(tags=["Roles"])


class RoleAssignmentRequest(BaseModel):
    user_id: str
    tenant_id: str
    role_names: list[str] = Field(min_length=1)


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)  # Descope role names accept up to 255 chars; standardized across all routers
    description: str = Field(default="", max_length=1000)  # Reasonable UX limit for human-readable descriptions
    permission_names: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list, max_length=100)  # A role can have up to 100 permissions — generous but bounded


class UpdateRoleRequest(BaseModel):
    new_name: str | None = Field(default=None, min_length=1, max_length=255)  # Descope role names accept up to 255 chars; standardized across all routers
    description: str | None = Field(default=None, max_length=1000)  # Reasonable UX limit for human-readable descriptions
    permission_names: list[Annotated[str, Field(min_length=1)]] | None = Field(default=None, max_length=100)  # A role can have up to 100 permissions — generous but bounded


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    """Parse a string to UUID, raising 422 on invalid input."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid UUID for {field_name}: {value}")


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


@router.post("/roles/remove")
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


@router.get("/roles")
async def list_roles(
    request: Request,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    role_service: RoleService = Depends(get_role_service),
):
    """List all role definitions. Requires owner or admin role."""
    result = await role_service.list_roles()
    if result.is_ok():
        result = Ok({"roles": result.ok})
    return result_to_response(result, request)


@router.post("/roles", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_role(
    request: Request,
    body: CreateRoleRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    role_service: RoleService = Depends(get_role_service),
):
    """Create a new role definition. Requires owner or admin role."""
    result = await role_service.create_role(
        name=body.name,
        description=body.description,
        permission_names=body.permission_names or None,
    )
    return result_to_response(result, request, status=201)


@router.put("/roles/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_role(
    request: Request,
    name: str,
    body: UpdateRoleRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
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


@router.delete("/roles/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_role(
    request: Request,
    name: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
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
