import uuid

from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.dependencies.identity import get_role_service, get_user_service
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.role import RoleService
from app.services.user import UserService

router = APIRouter(tags=["Users"])


class InviteUserRequest(BaseModel):
    email: EmailStr
    role_names: list[str] = Field(default_factory=lambda: ["member"])


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    """Parse a string to UUID, raising 422 on invalid input."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid UUID for {field_name}: {value}")


@router.get("/members")
async def list_members(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    user_service: UserService = Depends(get_user_service),
):
    """List all members in the current tenant."""
    tenant_uuid = _parse_uuid(tenant_id, "tenant_id")
    result = await user_service.search_users(tenant_id=tenant_uuid)
    if result.is_ok():
        result = Ok({"members": result.ok})
    return result_to_response(result, request)


@router.post("/members/invite")
@limiter.limit(RATE_LIMIT_AUTH)
async def invite_member(
    request: Request,
    body: InviteUserRequest,
    tenant_id: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
    user_service: UserService = Depends(get_user_service),
    role_service: RoleService = Depends(get_role_service),
):
    """Invite a user to the current tenant by email with specified roles."""
    if "owner" in body.role_names and "owner" not in caller_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")
    tenant_uuid = _parse_uuid(tenant_id, "tenant_id")
    result = await user_service.create_user(
        tenant_id=tenant_uuid,
        email=body.email,
        user_name=body.email,
    )
    if result.is_error():
        return result_to_response(result, request)

    user_data = result.ok

    # Assign requested roles to the new user (best-effort: user is created regardless)
    if body.role_names:
        user_uuid = uuid.UUID(str(user_data["id"]))
        roles_result = await role_service.list_roles()
        if roles_result.is_ok():
            role_map = {r["name"]: r["id"] for r in roles_result.ok}
            for role_name in body.role_names:
                role_id = role_map.get(role_name)
                if role_id is not None:
                    assign_result = await role_service.assign_role_to_user(
                        user_id=user_uuid,
                        tenant_id=tenant_uuid,
                        role_id=uuid.UUID(str(role_id)),
                    )
                    if assign_result.is_error():
                        break  # stop on first failure, user still created

    return result_to_response(
        Ok({"status": "invited", "email": body.email, "user": user_data}),
        request,
    )


@router.post("/members/{user_id}/deactivate")
async def deactivate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    user_service: UserService = Depends(get_user_service),
):
    """Deactivate a member. They will not be able to log in."""
    tenant_uuid = _parse_uuid(tenant_id, "tenant_id")
    user_uuid = _parse_uuid(user_id, "user_id")
    result = await user_service.deactivate_user(
        tenant_id=tenant_uuid,
        user_id=user_uuid,
    )
    if result.is_ok():
        result = Ok({"status": "deactivated", "user_id": user_id})
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
    tenant_uuid = _parse_uuid(tenant_id, "tenant_id")
    user_uuid = _parse_uuid(user_id, "user_id")
    result = await user_service.activate_user(
        tenant_id=tenant_uuid,
        user_id=user_uuid,
    )
    if result.is_ok():
        result = Ok({"status": "activated", "user_id": user_id})
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
    tenant_uuid = _parse_uuid(tenant_id, "tenant_id")
    user_uuid = _parse_uuid(user_id, "user_id")
    result = await user_service.remove_user_from_tenant(
        tenant_id=tenant_uuid,
        user_id=user_uuid,
    )
    if result.is_ok():
        result = Ok({"status": "removed", "user_id": user_id})
    return result_to_response(result, request)
