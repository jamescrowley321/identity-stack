from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.descope import get_descope_client

router = APIRouter(tags=["Users"])


class InviteUserRequest(BaseModel):
    email: EmailStr
    role_names: list[str] = Field(default_factory=lambda: ["member"])


async def _verify_user_tenant(user_id: str, tenant_id: str) -> tuple[dict, str]:
    """Load a user, verify tenant membership, return (user, login_id).

    Descope mutation endpoints require loginId, but the frontend sends userId.
    This resolves the loginId from the user object.
    """
    client = get_descope_client()
    user = await client.load_user(user_id)
    user_tenants = [t.get("tenantId", "") for t in user.get("userTenants", [])] if user.get("userTenants") else []
    if tenant_id not in user_tenants:
        raise HTTPException(status_code=403, detail="User does not belong to your tenant")
    login_ids = user.get("loginIds", [])
    if not login_ids:
        raise HTTPException(status_code=400, detail="User has no login identifiers")
    return user, login_ids[0]


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
@limiter.limit(RATE_LIMIT_AUTH)
async def invite_member(
    request: Request,
    body: InviteUserRequest,
    tenant_id: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Invite a user to the current tenant by email with specified roles."""
    if "owner" in body.role_names and "owner" not in caller_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")
    client = get_descope_client()
    user = await client.invite_user(body.email, tenant_id, body.role_names)
    return {"status": "invited", "email": body.email, "user": user}


@router.post("/members/{user_id}/deactivate")
async def deactivate_member(
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Deactivate a member. They will not be able to log in."""
    _, login_id = await _verify_user_tenant(user_id, tenant_id)
    client = get_descope_client()
    await client.update_user_status(login_id, "disabled")
    return {"status": "deactivated", "user_id": user_id}


@router.post("/members/{user_id}/activate")
async def activate_member(
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Reactivate a previously deactivated member."""
    _, login_id = await _verify_user_tenant(user_id, tenant_id)
    client = get_descope_client()
    await client.update_user_status(login_id, "enabled")
    return {"status": "activated", "user_id": user_id}


@router.delete("/members/{user_id}")
async def remove_member(
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Remove a member from the current tenant."""
    _, login_id = await _verify_user_tenant(user_id, tenant_id)
    client = get_descope_client()
    await client.remove_user_from_tenant(login_id, tenant_id)
    return {"status": "removed", "user_id": user_id}
