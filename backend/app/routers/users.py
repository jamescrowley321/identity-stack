import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from app.dependencies.identity import get_identity_service
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.identity import IdentityService

router = APIRouter(tags=["Users"])


class InviteUserRequest(BaseModel):
    email: EmailStr
    role_names: list[str] = Field(default_factory=lambda: ["member"])


def _parse_uuid(value: str, name: str) -> uuid.UUID:
    """Parse a string to UUID at the router boundary."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {name} format")


@router.get("/members")
async def list_members(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """List all members in the current tenant."""
    tid = _parse_uuid(tenant_id, "tenant_id")
    result = await service.get_tenant_users_with_roles(tenant_id=tid)
    if result.is_error():
        return result_to_response(result, request)
    return {"members": result.ok}


@router.post("/members/invite")
@limiter.limit(RATE_LIMIT_AUTH)
async def invite_member(
    request: Request,
    body: InviteUserRequest,
    tenant_id: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Invite a user to the current tenant by email with specified roles."""
    if "owner" in body.role_names and "owner" not in caller_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")
    tid = _parse_uuid(tenant_id, "tenant_id")

    create_result = await service.create_user(tenant_id=tid, email=body.email, user_name=body.email)
    if create_result.is_error():
        return result_to_response(create_result, request)
    user_dict = create_result.ok
    user_id = uuid.UUID(user_dict["id"])

    for role_name in body.role_names:
        role_result = await service.get_role_by_name(name=role_name)
        if role_result.is_error():
            return result_to_response(role_result, request)
        role_id = uuid.UUID(role_result.ok["id"])
        assign_result = await service.assign_role_to_user(tenant_id=tid, user_id=user_id, role_id=role_id)
        if assign_result.is_error():
            return result_to_response(assign_result, request)

    return JSONResponse(
        content={"status": "invited", "email": body.email, "user": user_dict},
        status_code=201,
    )


@router.post("/members/{user_id}/deactivate")
async def deactivate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Deactivate a member. They will not be able to log in."""
    tid = _parse_uuid(tenant_id, "tenant_id")
    uid = _parse_uuid(user_id, "user_id")
    result = await service.deactivate_user(tenant_id=tid, user_id=uid)
    if result.is_error():
        return result_to_response(result, request)
    return {"status": "deactivated", "user_id": user_id}


@router.post("/members/{user_id}/activate")
async def activate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Reactivate a previously deactivated member."""
    tid = _parse_uuid(tenant_id, "tenant_id")
    uid = _parse_uuid(user_id, "user_id")
    result = await service.activate_user(tenant_id=tid, user_id=uid)
    if result.is_error():
        return result_to_response(result, request)
    return {"status": "activated", "user_id": user_id}


@router.delete("/members/{user_id}")
async def remove_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Remove a member from the current tenant."""
    tid = _parse_uuid(tenant_id, "tenant_id")
    uid = _parse_uuid(user_id, "user_id")
    result = await service.remove_user_from_tenant(tenant_id=tid, user_id=uid)
    if result.is_error():
        return result_to_response(result, request)
    return {"status": "removed", "user_id": user_id}
