from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies.auth import get_claims
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.services.descope import get_descope_client

router = APIRouter()

AttributeValue = str | int | bool | float | None


class UpdateAttributeRequest(BaseModel):
    key: str
    value: AttributeValue


class UpdateTenantSettingsRequest(BaseModel):
    custom_attributes: dict[str, AttributeValue]


@router.get("/profile")
async def get_profile(claims: dict = Depends(get_claims)):
    """Load the current user's profile and custom attributes from Descope."""
    user_id = claims.get("sub")
    if not user_id:
        return {"user": None, "custom_attributes": {}}
    try:
        client = get_descope_client()
        user = await client.load_user(user_id)
        return {
            "user_id": user_id,
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "custom_attributes": user.get("customAttributes", {}),
        }
    except Exception:
        return {"user_id": user_id, "name": "", "email": "", "custom_attributes": {}}


@router.patch("/profile")
async def update_profile_attribute(
    body: UpdateAttributeRequest,
    claims: dict = Depends(get_claims),
):
    """Update a single custom attribute on the current user's profile."""
    user_id = claims.get("sub", "")
    client = get_descope_client()
    await client.update_user_custom_attribute(user_id, body.key, body.value)
    return {"status": "updated", "key": body.key, "value": body.value}


@router.get("/tenants/current/settings")
async def get_tenant_settings(tenant_id: str = Depends(get_tenant_id)):
    """Load the current tenant's settings and custom attributes from Descope."""
    try:
        client = get_descope_client()
        tenant = await client.load_tenant(tenant_id)
        return {
            "tenant_id": tenant_id,
            "name": tenant.get("name", ""),
            "custom_attributes": tenant.get("customAttributes", {}),
        }
    except Exception:
        return {"tenant_id": tenant_id, "name": "", "custom_attributes": {}}


@router.patch("/tenants/current/settings")
async def update_tenant_settings(
    body: UpdateTenantSettingsRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Update custom attributes on the current tenant. Requires owner or admin role."""
    client = get_descope_client()
    await client.update_tenant_custom_attributes(tenant_id, body.custom_attributes)
    return {"status": "updated", "tenant_id": tenant_id, "custom_attributes": body.custom_attributes}
