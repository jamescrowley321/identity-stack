from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter

router = APIRouter(tags=["Access Keys"])


class CreateAccessKeyRequest(BaseModel):
    name: str
    expire_time: int | None = None
    role_names: list[str] | None = None


async def _verify_key_tenant(request: Request, key_id: str, tenant_id: str) -> dict:
    """Load a key and verify it belongs to the caller's tenant."""
    key = await request.app.state.descope_client.load_access_key(key_id)
    key_tenants = [t.get("tenantId", "") for t in key.get("keyTenants", [])] if key.get("keyTenants") else []
    if tenant_id not in key_tenants:
        raise HTTPException(status_code=403, detail="Key does not belong to your tenant")
    return key


@router.post("/keys")
@limiter.limit(RATE_LIMIT_AUTH)
async def create_access_key(
    request: Request,
    body: CreateAccessKeyRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Create an access key scoped to the current tenant. Returns cleartext (shown once)."""
    client = request.app.state.descope_client
    result = await client.create_access_key(
        name=body.name,
        tenant_id=tenant_id,
        expire_time=body.expire_time,
        role_names=body.role_names,
    )
    return result


@router.get("/keys")
async def list_access_keys(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List access keys for the current tenant."""
    client = request.app.state.descope_client
    keys = await client.search_access_keys(tenant_id)
    return {"keys": keys}


@router.get("/keys/{key_id}")
async def get_access_key(
    request: Request,
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Load a single access key by ID. Verifies key belongs to current tenant."""
    key = await _verify_key_tenant(request, key_id, tenant_id)
    return key


@router.post("/keys/{key_id}/deactivate")
async def deactivate_access_key(
    request: Request,
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Deactivate (revoke) an access key. Verifies key belongs to current tenant."""
    await _verify_key_tenant(request, key_id, tenant_id)
    client = request.app.state.descope_client
    await client.deactivate_access_key(key_id)
    return {"status": "deactivated", "key_id": key_id}


@router.post("/keys/{key_id}/activate")
async def activate_access_key(
    request: Request,
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Reactivate a previously deactivated access key. Verifies key belongs to current tenant."""
    await _verify_key_tenant(request, key_id, tenant_id)
    client = request.app.state.descope_client
    await client.activate_access_key(key_id)
    return {"status": "activated", "key_id": key_id}


@router.delete("/keys/{key_id}")
async def delete_access_key(
    request: Request,
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Permanently delete an access key. Verifies key belongs to current tenant."""
    await _verify_key_tenant(request, key_id, tenant_id)
    client = request.app.state.descope_client
    await client.delete_access_key(key_id)
    return {"status": "deleted", "key_id": key_id}
