from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.services.descope import get_descope_client

router = APIRouter()

# M3: Constrain to known Descope role names
ValidRole = Literal["owner", "admin", "member", "viewer"]

# M1: Role hierarchy — higher number = more privilege
ROLE_HIERARCHY: dict[str, int] = {"owner": 4, "admin": 3, "member": 2, "viewer": 1}


def _check_role_hierarchy(caller_roles: list[str], target_roles: list[ValidRole]) -> None:
    """Ensure caller's highest role outranks every target role.

    Owners can manage any role. All others can only manage roles strictly
    below their own level. Raises HTTPException 403 on violation.
    """
    caller_max = max((ROLE_HIERARCHY.get(r, 0) for r in caller_roles), default=0)
    if caller_max >= ROLE_HIERARCHY["owner"]:
        return  # owners can assign/remove any role
    for role in target_roles:
        if ROLE_HIERARCHY.get(role, 0) >= caller_max:
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient privileges to manage the '{role}' role",
            )


class RoleAssignmentRequest(BaseModel):
    user_id: str
    tenant_id: str
    role_names: list[ValidRole] = Field(min_length=1)


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
async def assign_roles(
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Assign roles to a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    _check_role_hierarchy(caller_roles, body.role_names)
    client = get_descope_client()
    try:
        await client.assign_roles(body.user_id, body.tenant_id, list(body.role_names))
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            raise HTTPException(status_code=400, detail="Role operation failed: invalid request")
        raise HTTPException(status_code=502, detail="Role operation failed: upstream service error")
    return {"status": "roles_assigned", "user_id": body.user_id, "role_names": body.role_names}


@router.post("/roles/remove")
async def remove_roles(
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Remove roles from a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    _check_role_hierarchy(caller_roles, body.role_names)
    client = get_descope_client()
    try:
        await client.remove_roles(body.user_id, body.tenant_id, list(body.role_names))
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            raise HTTPException(status_code=400, detail="Role operation failed: invalid request")
        raise HTTPException(status_code=502, detail="Role operation failed: upstream service error")
    return {"status": "roles_removed", "user_id": body.user_id, "role_names": body.role_names}
