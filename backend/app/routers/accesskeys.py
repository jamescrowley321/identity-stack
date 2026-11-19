"""``/api/keys``: the ``machine_credentials`` capability (conventions §18).

Each route answers through ``MachineCredentialService``, which hands the request
to the deployment's machine credentials: Descope access keys, or a canonical
provider's (``app.services.machine_credentials``). The capability gate goes first.
For a Descope deployment it serves Descope principals only, and the Descope
builder refuses any other principal again before Descope is called
(``require_descope_principal``).
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.dependencies.capabilities import require_machine_credentials
from app.dependencies.identity import get_machine_credential_service
from app.dependencies.rbac import refuse_machine_principal, require_role
from app.dependencies.tenant import get_tenant_id
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.machine_credentials import MachineCredentialService

# Each route takes its service before its tenant: a Descope deployment's builder
# refuses another provider's principal before the tenant is resolved (§18).
# A machine credential's own token is refused on every route here, after the
# capability gate: no credential creates, renews or manages credentials.
router = APIRouter(
    tags=["Access Keys"],
    dependencies=[Depends(require_machine_credentials), Depends(refuse_machine_principal)],
)


class CreateAccessKeyRequest(BaseModel):
    name: str
    expire_time: int | None = None
    role_names: list[str] | None = None


@router.post("/keys")
@limiter.limit(RATE_LIMIT_AUTH)
async def create_access_key(
    request: Request,
    body: CreateAccessKeyRequest,
    service: MachineCredentialService = Depends(get_machine_credential_service),
    tenant_id: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Create a machine credential of the current tenant. Returns its secret (``cleartext``), shown once.

    The credential may carry only roles the caller holds in the tenant: a
    credential outlives the membership that created it and is exchanged for
    tokens carrying its roles, so granting more would be an escalation and a
    persistence path. ``expire_time`` is required, in the future, and at most
    365 days ahead (``app.domain.credentials``).
    """
    result = await service.create(
        tenant_id=tenant_id,
        name=body.name,
        expire_time=body.expire_time,
        role_names=body.role_names,
        caller_roles=caller_roles,
    )
    return result_to_response(result, request)


@router.get("/keys")
async def list_access_keys(
    request: Request,
    service: MachineCredentialService = Depends(get_machine_credential_service),
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List the current tenant's machine credentials."""
    return result_to_response(await service.list(tenant_id=tenant_id), request)


@router.get("/keys/{key_id}")
async def get_access_key(
    request: Request,
    key_id: str,
    service: MachineCredentialService = Depends(get_machine_credential_service),
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Load one machine credential of the current tenant."""
    return result_to_response(await service.get(tenant_id=tenant_id, credential_id=key_id), request)


@router.post("/keys/{key_id}/deactivate")
async def deactivate_access_key(
    request: Request,
    key_id: str,
    service: MachineCredentialService = Depends(get_machine_credential_service),
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Deactivate (revoke) a machine credential of the current tenant: its tokens are refused."""
    return result_to_response(await service.deactivate(tenant_id=tenant_id, credential_id=key_id), request)


@router.post("/keys/{key_id}/activate")
async def activate_access_key(
    request: Request,
    key_id: str,
    service: MachineCredentialService = Depends(get_machine_credential_service),
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Reactivate a deactivated machine credential of the current tenant."""
    return result_to_response(await service.activate(tenant_id=tenant_id, credential_id=key_id), request)


@router.delete("/keys/{key_id}")
async def delete_access_key(
    request: Request,
    key_id: str,
    service: MachineCredentialService = Depends(get_machine_credential_service),
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Permanently delete a machine credential of the current tenant."""
    return result_to_response(await service.delete(tenant_id=tenant_id, credential_id=key_id), request)
