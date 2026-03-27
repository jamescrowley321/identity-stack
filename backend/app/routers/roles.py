from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.services.descope import get_descope_client

router = APIRouter()


class RoleAssignmentRequest(BaseModel):
    user_id: str
    tenant_id: str
    role_names: list[str] = Field(min_length=1)


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
    admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Assign roles to a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    if "owner" in body.role_names and "owner" not in admin_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")
    client = get_descope_client()
    await client.assign_roles(body.user_id, body.tenant_id, body.role_names)
    return {"status": "roles_assigned", "user_id": body.user_id, "role_names": body.role_names}


@router.post("/roles/remove")
async def remove_roles(
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Remove roles from a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    if "owner" in body.role_names and "owner" not in admin_roles:
        raise HTTPException(status_code=403, detail="Only owners can remove the owner role")
    client = get_descope_client()
    await client.remove_roles(body.user_id, body.tenant_id, body.role_names)
    return {"status": "roles_removed", "user_id": body.user_id, "role_names": body.role_names}
