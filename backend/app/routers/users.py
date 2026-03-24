from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.services.descope import get_descope_client

router = APIRouter()


class InviteUserRequest(BaseModel):
    email: str
    role_names: list[str] = Field(default_factory=lambda: ["member"])


@router.get("/members")
async def list_members(
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List all members in the current tenant."""
    client = get_descope_client()
    users = await client.search_tenant_users(tenant_id)
    return {"members": users}


@router.post("/members/invite")
async def invite_member(
    body: InviteUserRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Invite a user to the current tenant by email with specified roles."""
    client = get_descope_client()
    user = await client.invite_user(body.email, tenant_id, body.role_names)
    return {"status": "invited", "email": body.email, "user": user}


@router.post("/members/{user_id}/deactivate")
async def deactivate_member(
    user_id: str,
    _tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Deactivate a member. They will not be able to log in."""
    client = get_descope_client()
    await client.update_user_status(user_id, "disabled")
    return {"status": "deactivated", "user_id": user_id}


@router.post("/members/{user_id}/activate")
async def activate_member(
    user_id: str,
    _tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Reactivate a previously deactivated member."""
    client = get_descope_client()
    await client.update_user_status(user_id, "enabled")
    return {"status": "activated", "user_id": user_id}


@router.delete("/members/{user_id}")
async def remove_member(
    user_id: str,
    _tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Remove a member permanently."""
    client = get_descope_client()
    await client.delete_user(user_id)
    return {"status": "removed", "user_id": user_id}
