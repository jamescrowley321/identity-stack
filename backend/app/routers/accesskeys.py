from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.services.descope import get_descope_client

router = APIRouter()


class CreateAccessKeyRequest(BaseModel):
    name: str
    expire_time: int | None = None
    role_names: list[str] | None = None


@router.post("/keys")
async def create_access_key(
    body: CreateAccessKeyRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Create an access key scoped to the current tenant. Returns cleartext (shown once)."""
    client = get_descope_client()
    result = await client.create_access_key(
        name=body.name,
        tenant_id=tenant_id,
        expire_time=body.expire_time,
        role_names=body.role_names,
    )
    return result


@router.get("/keys")
async def list_access_keys(
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List access keys for the current tenant."""
    client = get_descope_client()
    keys = await client.search_access_keys(tenant_id)
    return {"keys": keys}


@router.get("/keys/{key_id}")
async def get_access_key(
    key_id: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Load a single access key by ID."""
    client = get_descope_client()
    key = await client.load_access_key(key_id)
    return key


@router.post("/keys/{key_id}/deactivate")
async def deactivate_access_key(
    key_id: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Deactivate (revoke) an access key."""
    client = get_descope_client()
    await client.deactivate_access_key(key_id)
    return {"status": "deactivated", "key_id": key_id}


@router.post("/keys/{key_id}/activate")
async def activate_access_key(
    key_id: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Reactivate a previously deactivated access key."""
    client = get_descope_client()
    await client.activate_access_key(key_id)
    return {"status": "activated", "key_id": key_id}


@router.delete("/keys/{key_id}")
async def delete_access_key(
    key_id: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Permanently delete an access key."""
    client = get_descope_client()
    await client.delete_access_key(key_id)
    return {"status": "deleted", "key_id": key_id}
