from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.dependencies.identity import get_member_directory
from app.dependencies.rbac import OperatorGrantCheck, operator_grant_check, require_role
from app.dependencies.sync_provider import require_sync_provider_principal
from app.dependencies.tenant import get_tenant_id
from app.errors.identity import Conflict, Forbidden, IdentityError, ValidationError
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.adapters.base import PROVIDER_SYNC
from app.services.members import MemberDirectory
from app.services.user import MemberStatus

# The members live where the deployment syncs to (``get_member_directory``): in
# Descope under Descope's ids, or in the canonical model, whose changes the sync
# adapter pushes and takes the request's tenant and user ids as its provider's. So
# the router serves the sync provider's principals only (conventions §18).
router = APIRouter(tags=["Users"], dependencies=[Depends(require_sync_provider_principal)])


class InviteUserRequest(BaseModel):
    email: EmailStr
    role_names: list[str] = Field(default_factory=lambda: ["member"])


# A status is the account's, not one tenant membership's, in Descope and in the
# canonical model alike (#581).
_SHARED_ACCOUNT_DETAIL = (
    "Status applies to the member's whole account, which also belongs to another tenant. "
    "Remove the member from this tenant instead."
)


def _refusal(error: IdentityError, failed: str, conflict_detail: str | None = None) -> HTTPException:
    """The answer to a member operation that did not happen.

    400 for a request the directory rejects, 403 for a user who is no member of
    the tenant, 409 for a conflict (``conflict_detail`` when given), and 502 with
    ``failed`` for anything else: the provider failed, or (Descope) reads an
    outsider like an unknown user.
    """
    if isinstance(error, ValidationError):
        return HTTPException(status_code=400, detail=error.message)
    if isinstance(error, Forbidden):
        return HTTPException(status_code=403, detail=error.message)
    if isinstance(error, Conflict):
        return HTTPException(status_code=409, detail=conflict_detail or error.message)
    return HTTPException(status_code=502, detail=failed)


def _answer(status: str, user_id: str, outcome: dict | None) -> dict:
    """The answer to a status change or a removal, with ``provider_sync`` when the service reported one."""
    answer = {"status": status, "user_id": user_id}
    if outcome and PROVIDER_SYNC in outcome:
        answer[PROVIDER_SYNC] = outcome[PROVIDER_SYNC]
    return answer


@router.get("/members")
async def list_members(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    members: MemberDirectory = Depends(get_member_directory),
):
    """List the members of the current tenant, each with its roles there."""
    result = await members.list_members(tenant_id)
    if result.is_error():
        raise _refusal(result.error, "Failed to list members")
    return result_to_response(result, request)


@router.post("/members/invite")
@limiter.limit(RATE_LIMIT_AUTH)
async def invite_member(
    request: Request,
    body: InviteUserRequest,
    tenant_id: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
    check_operator_grant: OperatorGrantCheck = Depends(operator_grant_check),
    members: MemberDirectory = Depends(get_member_directory),
):
    """Invite a user to the current tenant by email with specified roles."""
    if "owner" in body.role_names and "owner" not in caller_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")
    await check_operator_grant(body.role_names)

    result = await members.invite_member(email=body.email, tenant_id=tenant_id, role_names=body.role_names)
    if result.is_error():
        raise _refusal(result.error, "Failed to invite member")
    return result_to_response(result, request)


@router.post("/members/{user_id}/deactivate")
async def deactivate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    members: MemberDirectory = Depends(get_member_directory),
):
    """Deactivate a member. They will not be able to log in."""
    result = await members.set_member_status(user_id, tenant_id, MemberStatus.DISABLED)
    if result.is_error():
        raise _refusal(result.error, "Failed to deactivate member", _SHARED_ACCOUNT_DETAIL)
    return result_to_response(Ok(_answer("deactivated", user_id, result.ok)), request)


@router.post("/members/{user_id}/activate")
async def activate_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    members: MemberDirectory = Depends(get_member_directory),
):
    """Reactivate a previously deactivated member."""
    result = await members.set_member_status(user_id, tenant_id, MemberStatus.ENABLED)
    if result.is_error():
        raise _refusal(result.error, "Failed to activate member", _SHARED_ACCOUNT_DETAIL)
    return result_to_response(Ok(_answer("activated", user_id, result.ok)), request)


@router.delete("/members/{user_id}")
async def remove_member(
    request: Request,
    user_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    members: MemberDirectory = Depends(get_member_directory),
):
    """Remove a member from the current tenant."""
    result = await members.remove_member(user_id, tenant_id)
    if result.is_error():
        raise _refusal(result.error, "Failed to remove member")
    return result_to_response(Ok(_answer("removed", user_id, result.ok)), request)
