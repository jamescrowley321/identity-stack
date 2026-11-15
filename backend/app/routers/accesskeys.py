import time

from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies.descope import get_descope_management
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.descope_management import DescopeManagementAdapter

router = APIRouter(tags=["Access Keys"])

# Longest lifetime an access key may be given, in seconds. `expireTime` is epoch
# seconds and Descope treats 0 (and an absent value) as "never expires", so
# without a ceiling the default was a permanent credential. 365 days matches the
# longest option the UI already offered; tighten by lowering this constant.
MAX_ACCESS_KEY_LIFETIME_SECONDS = 365 * 24 * 60 * 60


class CreateAccessKeyRequest(BaseModel):
    name: str
    expire_time: int | None = None
    role_names: list[str] | None = None


async def _verify_key_tenant(management: DescopeManagementAdapter, key_id: str, tenant_id: str) -> dict:
    """Load a key and verify it belongs to the caller's tenant."""
    result = await management.load_tenant_access_key(key_id, tenant_id)
    if result.is_error():
        raise HTTPException(status_code=403, detail="Key does not belong to your tenant")
    return result.ok


def _require_roles_caller_holds(role_names: list[str] | None, caller_roles: list[str]) -> None:
    """Refuse to mint a key carrying a role the caller does not hold.

    The previous guard special-cased ``owner`` only, so an ``admin`` could put any
    other role — including custom ones granting more than the caller has — on a
    key, exchange the cleartext for a session token whose ``tenants`` claim
    carries that role, and act with privileges it was never granted. Containment
    covers ``owner`` as a special case of the general rule, so the old check is
    subsumed rather than removed.
    """
    requested = set(role_names or [])
    if not requested:
        return
    escalated = sorted(requested - set(caller_roles))
    if escalated:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot grant roles you do not hold: {', '.join(escalated)}",
        )


def _validated_expire_time(expire_time: int | None) -> int:
    """Require a bounded, future expiry.

    ``expireTime`` is epoch seconds, and Descope treats 0 — or an absent value —
    as never expiring. Both used to be accepted, and omitting the field was the
    UI's default, so the ordinary way to create a key produced a permanent
    credential that outlives the membership of whoever minted it.
    """
    if expire_time is None or expire_time == 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "expire_time is required and must be a future epoch-seconds timestamp; "
                "a key that never expires outlives the membership that created it"
            ),
        )

    now = int(time.time())
    if expire_time <= now:
        raise HTTPException(status_code=422, detail="expire_time must be in the future")

    max_allowed = now + MAX_ACCESS_KEY_LIFETIME_SECONDS
    if expire_time > max_allowed:
        max_days = MAX_ACCESS_KEY_LIFETIME_SECONDS // 86400
        raise HTTPException(
            status_code=422,
            detail=f"expire_time may be at most {max_days} days in the future",
        )

    return expire_time


@router.post("/keys")
@limiter.limit(RATE_LIMIT_AUTH)
async def create_access_key(
    request: Request,
    body: CreateAccessKeyRequest,
    tenant_id: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Create an access key scoped to the current tenant. Returns cleartext (shown once).

    Only an owner may mint a key carrying `owner`, mirroring the guard on
    `POST /members/invite`. Without it an `admin` could grant itself `owner` on a
    key, exchange the cleartext for a session token whose `tenants` claim carries
    that role, and return as an owner — making indirectly exactly the grant the
    invite route refuses to make directly. A key also outlives the membership that
    created it and has caller-chosen expiry, so this is a persistence path as well
    as an escalation one.

    Granting roles the caller already holds stays allowed: scoping a key to a
    subset of your own roles is the normal use of this endpoint.

    Note that "or a lesser role" is deliberately *not* permitted, because this
    service has no way to know what "lesser" means. There is no role hierarchy
    (see issue #386), and authorization here gates on role *names*
    (``require_role("owner", "admin")``) rather than on the permissions a role
    carries — so a role with no permissions at all still unlocks every endpoint
    gated on its name. Comparing permission sets would therefore not be a sound
    substitute for containment.
    """
    _require_roles_caller_holds(body.role_names, caller_roles)
    expire_time = _validated_expire_time(body.expire_time)
    result = await management.create_access_key(
        name=body.name,
        tenant_id=tenant_id,
        expire_time=expire_time,
        role_names=body.role_names,
    )
    return result_to_response(result, request)


@router.get("/keys")
async def list_access_keys(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """List access keys for the current tenant."""
    return result_to_response(await management.list_access_keys(tenant_id), request)


@router.get("/keys/{key_id}")
async def get_access_key(
    request: Request,
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Load a single access key by ID. Verifies key belongs to current tenant."""
    key = await _verify_key_tenant(management, key_id, tenant_id)
    return result_to_response(Ok(key), request)


@router.post("/keys/{key_id}/deactivate")
async def deactivate_access_key(
    request: Request,
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Deactivate (revoke) an access key. Verifies key belongs to current tenant."""
    await _verify_key_tenant(management, key_id, tenant_id)
    result = await management.deactivate_access_key(key_id)
    if result.is_error():
        return result_to_response(result, request)
    return result_to_response(Ok({"status": "deactivated", "key_id": key_id}), request)


@router.post("/keys/{key_id}/activate")
async def activate_access_key(
    request: Request,
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Reactivate a previously deactivated access key. Verifies key belongs to current tenant."""
    await _verify_key_tenant(management, key_id, tenant_id)
    result = await management.activate_access_key(key_id)
    if result.is_error():
        return result_to_response(result, request)
    return result_to_response(Ok({"status": "activated", "key_id": key_id}), request)


@router.delete("/keys/{key_id}")
async def delete_access_key(
    request: Request,
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Permanently delete an access key. Verifies key belongs to current tenant."""
    await _verify_key_tenant(management, key_id, tenant_id)
    result = await management.delete_access_key(key_id)
    if result.is_error():
        return result_to_response(result, request)
    return result_to_response(Ok({"status": "deleted", "key_id": key_id}), request)
