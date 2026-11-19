from fastapi import APIRouter, Depends, HTTPException
from py_identity_model.identity import ClaimsPrincipal
from pydantic import BaseModel

from app.dependencies.auth import get_claims, get_current_user
from app.dependencies.capabilities import (
    custom_attributes_if_served,
    get_custom_attributes,
    require_custom_attributes,
)
from app.dependencies.identity import get_identity_resolution_service
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.logbook import logbook
from app.services.adapters.base import AttributeValue, CustomAttributes
from app.services.identity_resolution import IdentityResolutionService

logger = logbook(__name__)

router = APIRouter(tags=["Attributes"])

ALLOWED_USER_ATTRIBUTES = {"department", "job_title", "avatar_url"}
ALLOWED_TENANT_ATTRIBUTES = {"plan_tier", "max_members"}


class UpdateAttributeRequest(BaseModel):
    key: str
    value: AttributeValue


class UpdateTenantSettingsRequest(BaseModel):
    custom_attributes: dict[str, AttributeValue]


def _allowed(attributes: dict[str, AttributeValue], allowed: set[str]) -> dict[str, AttributeValue]:
    """Only the attributes the API serves: a provider may hold others (its own, a realm admin's)."""
    return {key: value for key, value in attributes.items() if key in allowed}


def _provider_name(principal: ClaimsPrincipal) -> str:
    """The name of the provider that authenticated the caller, lowercased (as ``GET /api/identity``)."""
    authentication_type = principal.identity.authentication_type if principal.identity else None
    return authentication_type.lower() if isinstance(authentication_type, str) else ""


@router.get("/profile")
async def get_profile(
    principal: ClaimsPrincipal = Depends(get_current_user),
    claims: dict = Depends(get_claims),
    resolver: IdentityResolutionService = Depends(get_identity_resolution_service),
    adapter: CustomAttributes | None = Depends(custom_attributes_if_served),
):
    """The caller's canonical name and email, and their custom attributes where the capability serves them.

    Every provider's session loads this page. ``user_id`` is the caller's ``sub``.
    Name and email are the canonical user's (empty when the caller has none or it
    cannot be read). The custom attributes come from the
    sync adapter only for a caller its ``custom_attributes`` capability serves
    (``served``: the sync provider's own principals), and are empty otherwise. A
    provider failure leaves them empty: the profile is informational.
    """
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Missing user identity (sub claim)")
    try:
        resolved = await resolver.resolve(provider=_provider_name(principal), sub=sub)
        user = resolved.ok["user"] if resolved.is_ok() else {}
    except Exception:
        logger.exception("canonical user of the profile not resolved")
        user = {}
    name = " ".join(part for part in (user.get("given_name"), user.get("family_name")) if part)
    attributes: dict[str, AttributeValue] = {}
    if adapter is not None:
        loaded = await adapter.user_attributes(subject=sub)
        if loaded.is_ok():
            attributes = _allowed(loaded.ok, ALLOWED_USER_ATTRIBUTES)
        else:
            logger.warning("custom attributes of the profile not loaded: %s", loaded.error.message)
    return {
        "user_id": sub,
        "name": name or user.get("user_name") or "",
        "email": user.get("email") or "",
        "custom_attributes": attributes,
    }


@router.patch("/profile", dependencies=[Depends(require_custom_attributes)])
async def update_profile_attribute(
    body: UpdateAttributeRequest,
    claims: dict = Depends(get_claims),
    adapter: CustomAttributes = Depends(get_custom_attributes),
):
    """Update a single custom attribute on the current user's profile."""
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=400, detail="Missing user identity (sub claim)")
    if body.key not in ALLOWED_USER_ATTRIBUTES:
        raise HTTPException(status_code=400, detail=f"Attribute '{body.key}' is not allowed")
    result = await adapter.set_user_attribute(subject=sub, key=body.key, value=body.value)
    if result.is_error():
        logger.warning("custom attribute %s not updated: %s", body.key, result.error.message)
        raise HTTPException(status_code=502, detail="Failed to update attribute in identity provider")
    return {"status": "updated", "key": body.key, "value": body.value}


@router.get("/tenants/current/settings", dependencies=[Depends(require_custom_attributes)])
async def get_tenant_settings(
    tenant_id: str = Depends(get_tenant_id),
    adapter: CustomAttributes = Depends(get_custom_attributes),
):
    """The current tenant's name and custom attributes, from the sync provider."""
    result = await adapter.tenant_attributes(tenant=tenant_id)
    if result.is_error():
        logger.warning("tenant settings not loaded: %s", result.error.message)
        raise HTTPException(status_code=502, detail="Failed to load tenant settings from identity provider")
    if result.ok is None:
        return {"tenant_id": tenant_id, "name": "", "custom_attributes": {}}
    return {
        "tenant_id": tenant_id,
        "name": result.ok.name,
        "custom_attributes": _allowed(result.ok.attributes, ALLOWED_TENANT_ATTRIBUTES),
    }


@router.patch("/tenants/current/settings", dependencies=[Depends(require_custom_attributes)])
async def update_tenant_settings(
    body: UpdateTenantSettingsRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    adapter: CustomAttributes = Depends(get_custom_attributes),
):
    """Update custom attributes on the current tenant. Requires owner or admin role."""
    disallowed = set(body.custom_attributes.keys()) - ALLOWED_TENANT_ATTRIBUTES
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=f"Attribute(s) not allowed: {', '.join(sorted(disallowed))}",
        )
    result = await adapter.set_tenant_attributes(tenant=tenant_id, attributes=body.custom_attributes)
    if result.is_error():
        logger.warning("tenant settings not updated: %s", result.error.message)
        raise HTTPException(status_code=502, detail="Failed to update tenant settings in identity provider")
    return {"status": "updated", "tenant_id": tenant_id, "custom_attributes": body.custom_attributes}
