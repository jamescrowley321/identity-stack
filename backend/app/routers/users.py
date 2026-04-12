import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter

router = APIRouter(tags=["Users"])
logger = logging.getLogger(__name__)


class InviteUserRequest(BaseModel):
    email: EmailStr
    role_names: list[str] = Field(default_factory=lambda: ["member"])


def _get_descope_client(request: Request):
    """Retrieve the DescopeManagementClient from app state."""
    client = getattr(request.app.state, "descope_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Descope client not initialized")
    return client


@router.get("/members")
async def list_members(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List all members in the current tenant via Descope Management API."""
    client = _get_descope_client(request)
    try:
        users = await client.search_tenant_users(tenant_id)
        members = []
        for u in users:
            tenant_roles = []
            for t in u.get("userTenants", []):
                if t.get("tenantId") == tenant_id:
                    tenant_roles = t.get("roleNames", [])
                    break
            members.append(
                {
                    "userId": u.get("userId", ""),
                    "email": u.get("email", ""),
                    "name": u.get("name", ""),
                    "status": u.get("status", "enabled"),
                    "roleNames": tenant_roles,
                }
            )
        return {"members": members}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error listing members: %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to list members from Descope") from exc


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

    client = _get_descope_client(request)
    try:
        result = await client.invite_user(
            email=body.email,
            tenant_id=tenant_id,
            role_names=body.role_names,
        )
        return {"status": "invited", "email": body.email, "user": result}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            detail = exc.response.json().get("errorDescription", "Bad request")
            raise HTTPException(status_code=400, detail=detail) from exc
        logger.warning("Descope API error inviting member: %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to invite member via Descope") from exc


@router.post("/members/{user_id}/deactivate")
async def deactivate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Deactivate a member. They will not be able to log in."""
    client = _get_descope_client(request)
    try:
        await client.update_user_status(user_id, "disabled")
        return {"status": "deactivated", "user_id": user_id}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error deactivating member: %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to deactivate member via Descope") from exc


@router.post("/members/{user_id}/activate")
async def activate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Reactivate a previously deactivated member."""
    client = _get_descope_client(request)
    try:
        await client.update_user_status(user_id, "enabled")
        return {"status": "activated", "user_id": user_id}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error activating member: %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to activate member via Descope") from exc


@router.delete("/members/{user_id}")
async def remove_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Remove a member from the current tenant."""
    client = _get_descope_client(request)
    try:
        await client.remove_user_from_tenant(user_id, tenant_id)
        return {"status": "removed", "user_id": user_id}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error removing member: %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to remove member via Descope") from exc
