from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.dependencies.descope import require_descope_management
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.errors.identity import Conflict, IdentityError, ValidationError
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter

router = APIRouter(tags=["Users"])


class InviteUserRequest(BaseModel):
    email: EmailStr
    role_names: list[str] = Field(default_factory=lambda: ["member"])


# Descope keeps one status per account, not per tenant membership (#581).
_SHARED_ACCOUNT_DETAIL = (
    "Status applies to the member's whole account, which also belongs to another tenant. "
    "Remove the member from this tenant instead."
)


def _refuse_shared_account(error: IdentityError) -> None:
    """Answer 409 when the member's account is shared with another tenant."""
    if isinstance(error, Conflict):
        raise HTTPException(status_code=409, detail=_SHARED_ACCOUNT_DETAIL)


@router.get("/members")
async def list_members(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List all members in the current tenant via Descope Management API."""
    result = await require_descope_management(request).list_members(tenant_id)
    if result.is_error():
        raise HTTPException(status_code=502, detail="Failed to list members from Descope")
    return result_to_response(result, request)


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

    result = await require_descope_management(request).invite_member(
        email=body.email,
        tenant_id=tenant_id,
        role_names=body.role_names,
    )
    if result.is_error():
        if isinstance(result.error, ValidationError):
            raise HTTPException(status_code=400, detail=result.error.message)
        raise HTTPException(status_code=502, detail="Failed to invite member via Descope")
    return result_to_response(result, request)


@router.post("/members/{user_id}/deactivate")
async def deactivate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Deactivate a member. They will not be able to log in."""
    result = await require_descope_management(request).set_member_status(user_id, tenant_id, "disabled")
    if result.is_error():
        _refuse_shared_account(result.error)
        raise HTTPException(status_code=502, detail="Failed to deactivate member via Descope")
    return result_to_response(Ok({"status": "deactivated", "user_id": user_id}), request)


@router.post("/members/{user_id}/activate")
async def activate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Reactivate a previously deactivated member."""
    result = await require_descope_management(request).set_member_status(user_id, tenant_id, "enabled")
    if result.is_error():
        _refuse_shared_account(result.error)
        raise HTTPException(status_code=502, detail="Failed to activate member via Descope")
    return result_to_response(Ok({"status": "activated", "user_id": user_id}), request)


@router.delete("/members/{user_id}")
async def remove_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Remove a member from the current tenant."""
    result = await require_descope_management(request).remove_member(user_id, tenant_id)
    if result.is_error():
        raise HTTPException(status_code=502, detail="Failed to remove member via Descope")
    return result_to_response(Ok({"status": "removed", "user_id": user_id}), request)
