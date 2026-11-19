import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from py_identity_model.identity import ClaimsPrincipal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.dependencies.auth import get_claims, get_current_user
from app.dependencies.capabilities import (
    custom_attributes_if_served,
    get_custom_attributes,
    require_custom_attributes,
    sync_provider_subject,
)
from app.dependencies.identity import get_identity_resolution_service, get_user_service
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.errors.identity import Conflict, NotFound
from app.logbook import logbook
from app.services.adapters.base import PROVIDER_SYNC, AttributeValue, CustomAttributes
from app.services.identity_resolution import IdentityResolutionService
from app.services.user import UserService

logger = logbook(__name__)

router = APIRouter(tags=["Attributes"])

ALLOWED_USER_ATTRIBUTES = {"department", "job_title", "avatar_url"}
ALLOWED_TENANT_ATTRIBUTES = {"plan_tier", "max_members"}

# The longest text an attribute or a name may be: Keycloak's user profile allows 255
# characters for a name; an attribute value is held to a URL's usual length.
MAX_NAME_LENGTH = 255
MAX_ATTRIBUTE_LENGTH = 2048

# An attribute value as a request may send it: no NaN or infinity, which JSON cannot
# carry and no provider stores, and no text longer than ``MAX_ATTRIBUTE_LENGTH``.
AttributeValueIn = (
    Annotated[str, StringConstraints(max_length=MAX_ATTRIBUTE_LENGTH)]
    | bool
    | int
    | Annotated[float, Field(allow_inf_nan=False)]
    | None
)


class UpdateAttributeRequest(BaseModel):
    key: str
    value: AttributeValueIn


class UpdateTenantSettingsRequest(BaseModel):
    custom_attributes: dict[str, AttributeValueIn]


class UpdateProfileRequest(BaseModel):
    """The caller's own names. The email is not here: sign-in and sync link accounts by it."""

    model_config = ConfigDict(extra="forbid")

    given_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH)]
    family_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_NAME_LENGTH)] = ""


def _allowed(attributes: dict[str, AttributeValue], allowed: set[str]) -> dict[str, AttributeValue]:
    """Only the attributes the API serves: a provider may hold others (its own, a realm admin's)."""
    return {key: value for key, value in attributes.items() if key in allowed}


def _provider_name(principal: ClaimsPrincipal) -> str:
    """The name of the provider that authenticated the caller, lowercased (as ``GET /api/identity``)."""
    authentication_type = principal.identity.authentication_type if principal.identity else None
    return authentication_type.lower() if isinstance(authentication_type, str) else ""


def _display_name(user: dict) -> str:
    name = " ".join(part for part in (user.get("given_name"), user.get("family_name")) if part)
    return name or user.get("user_name") or ""


def _subject(claims: dict, status_code: int) -> str:
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=status_code, detail="Missing user identity (sub claim)")
    return sub


@router.get("/profile")
async def get_profile(
    principal: ClaimsPrincipal = Depends(get_current_user),
    claims: dict = Depends(get_claims),
    resolver: IdentityResolutionService = Depends(get_identity_resolution_service),
    adapter: CustomAttributes | None = Depends(custom_attributes_if_served),
):
    """The caller's canonical names and email, and their custom attributes where the capability serves them.

    Every provider's session loads this page. ``user_id`` is the caller's ``sub``.
    Names and email are the canonical user's (empty when the caller has none or it
    cannot be read). The custom attributes come from the sync adapter only for a
    caller its ``custom_attributes`` capability serves (``served``: the sync
    provider's own principals), and are empty otherwise. A failure of either leaves
    its part empty: the profile is informational.
    """
    sub = _subject(claims, 401)
    try:
        resolved = await resolver.resolve(provider=_provider_name(principal), sub=sub)
        user = resolved.ok["user"] if resolved.is_ok() else {}
    except Exception:
        logger.exception("canonical user of the profile not resolved")
        user = {}
    attributes: dict[str, AttributeValue] = {}
    if adapter is not None:
        try:
            loaded = await adapter.user_attributes(subject=sub)
        except Exception:
            logger.exception("custom attributes of the profile not loaded")
        else:
            if loaded.is_ok():
                attributes = _allowed(loaded.ok, ALLOWED_USER_ATTRIBUTES)
            else:
                logger.warning("custom attributes of the profile not loaded: %s", loaded.error.message)
    return {
        "user_id": sub,
        "name": _display_name(user),
        "given_name": user.get("given_name") or "",
        "family_name": user.get("family_name") or "",
        "email": user.get("email") or "",
        "custom_attributes": attributes,
    }


@router.patch("/profile")
async def update_profile(
    body: UpdateProfileRequest,
    principal: ClaimsPrincipal = Depends(get_current_user),
    claims: dict = Depends(get_claims),
    resolver: IdentityResolutionService = Depends(get_identity_resolution_service),
    users: UserService = Depends(get_user_service),
    own_subject: str | None = Depends(sync_provider_subject),
):
    """Save the caller's own given and family names on the canonical user, and push them to the sync provider.

    Every provider's principal may. The names reach the caller's own account at the
    sync provider only when the caller is its principal (``sync_provider_subject``);
    ``provider_sync: failed`` in the answer says the push failed.
    """
    sub = _subject(claims, 400)
    resolved = await resolver.resolve(provider=_provider_name(principal), sub=sub)
    if resolved.is_error():
        raise HTTPException(status_code=404, detail="Identity not found")
    result = await users.update_own_names(
        user_id=uuid.UUID(str(resolved.ok["user"]["id"])),
        given_name=body.given_name,
        family_name=body.family_name,
        subject=own_subject,
    )
    if result.is_error():
        if isinstance(result.error, NotFound):
            raise HTTPException(status_code=404, detail="Identity not found")
        if isinstance(result.error, Conflict):
            raise HTTPException(status_code=409, detail=result.error.message)
        raise HTTPException(status_code=500, detail="Failed to update the profile")
    user = result.ok
    answer = {
        "status": "updated",
        "name": _display_name(user),
        "given_name": user["given_name"],
        "family_name": user["family_name"],
    }
    if PROVIDER_SYNC in user:
        answer[PROVIDER_SYNC] = user[PROVIDER_SYNC]
    return answer


@router.patch("/profile/attributes", dependencies=[Depends(require_custom_attributes)])
async def update_profile_attribute(
    body: UpdateAttributeRequest,
    claims: dict = Depends(get_claims),
    adapter: CustomAttributes = Depends(get_custom_attributes),
):
    """Update a single custom attribute on the current user's profile."""
    sub = _subject(claims, 400)
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
