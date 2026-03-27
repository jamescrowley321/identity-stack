import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.logging_config import get_logger
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.descope import DescopeManagementClient, get_descope_client

logger = get_logger(__name__)

router = APIRouter()


class CreateAccessKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expire_time: int | None = Field(default=None, gt=0)
    role_names: list[str] | None = None


async def _verify_key_tenant(key_id: str, tenant_id: str, client: DescopeManagementClient) -> dict:
    """Load a key and verify it belongs to the caller's tenant.

    Accepts an existing client to avoid creating a second connection for the
    subsequent mutation (addresses TOCTOU / double-client concern).
    """
    key = await client.load_access_key(key_id)
    key_tenants = [t.get("tenantId", "") for t in key.get("keyTenants", [])] if key.get("keyTenants") else []
    if tenant_id not in key_tenants:
        raise HTTPException(status_code=403, detail="Key does not belong to your tenant")
    return key


@router.post("/keys")
async def create_access_key(
    body: CreateAccessKeyRequest,
    tenant_id: str = Depends(get_tenant_id),
    caller_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Create an access key scoped to the current tenant. Returns cleartext (shown once)."""
    if body.role_names:
        escalated = set(body.role_names) - set(caller_roles)
        if escalated:
            raise HTTPException(
                status_code=403,
                detail=f"Cannot assign roles you do not hold: {', '.join(sorted(escalated))}",
            )
    try:
        client = get_descope_client()
        result = await client.create_access_key(
            name=body.name,
            tenant_id=tenant_id,
            expire_time=body.expire_time,
            role_names=body.role_names,
        )
        logger.info("accesskey.created name=%s tenant=%s", body.name, tenant_id)
        return result
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error creating access key: %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to create access key")


@router.get("/keys")
async def list_access_keys(
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List access keys for the current tenant."""
    try:
        client = get_descope_client()
        keys = await client.search_access_keys(tenant_id)
        return {"keys": keys}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error listing access keys: %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to list access keys")


@router.get("/keys/{key_id}")
async def get_access_key(
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Load a single access key by ID. Verifies key belongs to current tenant."""
    try:
        client = get_descope_client()
        key = await _verify_key_tenant(key_id, tenant_id, client)
        return key
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error loading access key %s: %s", key_id, exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to load access key")


@router.post("/keys/{key_id}/deactivate")
async def deactivate_access_key(
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Deactivate (revoke) an access key. Verifies key belongs to current tenant."""
    try:
        client = get_descope_client()
        await _verify_key_tenant(key_id, tenant_id, client)
        await client.deactivate_access_key(key_id)
        logger.info("accesskey.deactivated key_id=%s tenant=%s", key_id, tenant_id)
        return {"status": "deactivated", "key_id": key_id}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error deactivating access key %s: %s", key_id, exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to deactivate access key")


@router.post("/keys/{key_id}/activate")
async def activate_access_key(
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Reactivate a previously deactivated access key. Verifies key belongs to current tenant."""
    try:
        client = get_descope_client()
        await _verify_key_tenant(key_id, tenant_id, client)
        await client.activate_access_key(key_id)
        logger.info("accesskey.activated key_id=%s tenant=%s", key_id, tenant_id)
        return {"status": "activated", "key_id": key_id}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error activating access key %s: %s", key_id, exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to activate access key")


@router.delete("/keys/{key_id}")
async def delete_access_key(
    key_id: str,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Permanently delete an access key. Verifies key belongs to current tenant."""
    try:
        client = get_descope_client()
        await _verify_key_tenant(key_id, tenant_id, client)
        await client.delete_access_key(key_id)
        logger.info("accesskey.deleted key_id=%s tenant=%s", key_id, tenant_id)
        return {"status": "deleted", "key_id": key_id}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error deleting access key %s: %s", key_id, exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to delete access key")
