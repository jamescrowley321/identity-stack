import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.dependencies.identity import get_user_service
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.user import UserService

router = APIRouter(tags=["Users"])


class InviteUserRequest(BaseModel):
    email: EmailStr
    role_names: list[str] = Field(default_factory=lambda: ["member"])


@router.get("/members")
async def list_members(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    user_service: UserService = Depends(get_user_service),
):
    """List all members in the current tenant."""
    result = await user_service.search_users(tenant_id=uuid.UUID(tenant_id))
    return result_to_response(result, request)


@router.post("/members/invite")
@limiter.limit(RATE_LIMIT_AUTH)
async def invite_member(
    request: Request,
    body: InviteUserRequest,
    tenant_id: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
    user_service: UserService = Depends(get_user_service),
):
    """Invite a user to the current tenant by email with specified roles."""
    if "owner" in body.role_names and "owner" not in caller_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")
    result = await user_service.create_user(
        tenant_id=uuid.UUID(tenant_id),
        email=body.email,
        user_name=body.email,
    )
    return result_to_response(result, request, status=201)


@router.post("/members/{user_id}/deactivate")
async def deactivate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    user_service: UserService = Depends(get_user_service),
):
    """Deactivate a member. They will not be able to log in."""
    result = await user_service.deactivate_user(
        tenant_id=uuid.UUID(tenant_id),
        user_id=uuid.UUID(user_id),
    )
    return result_to_response(result, request)


@router.post("/members/{user_id}/activate")
async def activate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    user_service: UserService = Depends(get_user_service),
):
    """Reactivate a previously deactivated member."""
    result = await user_service.activate_user(
        tenant_id=uuid.UUID(tenant_id),
        user_id=uuid.UUID(user_id),
    )
    return result_to_response(result, request)


@router.delete("/members/{user_id}")
async def remove_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    user_service: UserService = Depends(get_user_service),
):
    """Remove a member from the current tenant."""
    result = await user_service.deactivate_user(
        tenant_id=uuid.UUID(tenant_id),
        user_id=uuid.UUID(user_id),
    )
    return result_to_response(result, request)
