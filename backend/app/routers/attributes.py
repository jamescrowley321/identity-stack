import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.dependencies.auth import get_claims
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.services.descope import get_descope_client

logger = logging.getLogger(__name__)

router = APIRouter()

AttributeValue = str | int | bool | float | None

ALLOWED_USER_ATTRIBUTES = {"department", "job_title", "avatar_url"}
ALLOWED_TENANT_ATTRIBUTES = {"plan_tier", "max_members"}


class UpdateAttributeRequest(BaseModel):
    key: str
    value: AttributeValue


class UpdateTenantSettingsRequest(BaseModel):
    custom_attributes: dict[str, AttributeValue]


@router.get("/profile")
async def get_profile(claims: dict = Depends(get_claims)):
    """Load the current user's profile and custom attributes from Descope."""
    # Descope's sub claim IS the loginId — verified via Descope JWT spec
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user identity (sub claim)")
    try:
        client = get_descope_client()
        user = await client.load_user(user_id)
        return {
            "user_id": user_id,
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "custom_attributes": user.get("customAttributes", {}),
        }
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error loading profile for %s: %s", user_id, exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to load user profile from identity provider")


@router.patch("/profile")
async def update_profile_attribute(
    body: UpdateAttributeRequest,
    claims: dict = Depends(get_claims),
):
    """Update a single custom attribute on the current user's profile."""
    # Descope's sub claim IS the loginId — verified via Descope JWT spec
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user identity (sub claim)")
    if body.key not in ALLOWED_USER_ATTRIBUTES:
        raise HTTPException(status_code=400, detail=f"Attribute '{body.key}' is not allowed")
    try:
        client = get_descope_client()
        await client.update_user_custom_attribute(user_id, body.key, body.value)
        return {"status": "updated", "key": body.key, "value": body.value}
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Descope API error updating attribute %s for %s: %s", body.key, user_id, exc.response.status_code
        )
        raise HTTPException(status_code=502, detail="Failed to update attribute in identity provider")


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
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error loading tenant %s: %s", tenant_id, exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to load tenant settings from identity provider")


@router.patch("/tenants/current/settings")
async def update_tenant_settings(
    body: UpdateTenantSettingsRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Update custom attributes on the current tenant. Requires owner or admin role."""
    disallowed = set(body.custom_attributes.keys()) - ALLOWED_TENANT_ATTRIBUTES
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=f"Attribute(s) not allowed: {', '.join(sorted(disallowed))}",
        )
    try:
        client = get_descope_client()
        await client.update_tenant_custom_attributes(tenant_id, body.custom_attributes)
        return {"status": "updated", "tenant_id": tenant_id, "custom_attributes": body.custom_attributes}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error updating tenant %s: %s", tenant_id, exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to update tenant settings in identity provider")
