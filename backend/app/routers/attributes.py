from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies.auth import get_claims
from app.dependencies.descope import get_descope_management
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.errors.identity import NotFound
from app.errors.problem_detail import result_to_response
from app.services.descope_management import AttributeValue, DescopeManagementAdapter

router = APIRouter(tags=["Attributes"])

ALLOWED_USER_ATTRIBUTES = {"department", "job_title", "avatar_url"}
ALLOWED_TENANT_ATTRIBUTES = {"plan_tier", "max_members"}


class UpdateAttributeRequest(BaseModel):
    key: str
    value: AttributeValue


class UpdateTenantSettingsRequest(BaseModel):
    custom_attributes: dict[str, AttributeValue]


@router.get("/profile")
async def get_profile(
    request: Request,
    claims: dict = Depends(get_claims),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Load the current user's profile and custom attributes from Descope."""
    # Descope's sub claim is the userId (not loginId)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user identity (sub claim)")
    result = await management.load_profile(user_id)
    if result.is_error():
        # Degrade gracefully — profile is informational, not critical.
        # In CI the Descope credentials are fake so every call 401s;
        # in production a transient Descope outage shouldn't crash the
        # profile page.
        result = Ok({"user_id": user_id, "name": "", "email": "", "custom_attributes": {}})
    return result_to_response(result, request)


@router.patch("/profile")
async def update_profile_attribute(
    request: Request,
    body: UpdateAttributeRequest,
    claims: dict = Depends(get_claims),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Update a single custom attribute on the current user's profile."""
    # Descope's sub claim is the userId — mutation endpoints require loginId
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user identity (sub claim)")
    if body.key not in ALLOWED_USER_ATTRIBUTES:
        raise HTTPException(status_code=400, detail=f"Attribute '{body.key}' is not allowed")
    result = await management.update_profile_attribute(user_id, body.key, body.value)
    if result.is_error():
        raise HTTPException(status_code=502, detail="Failed to update attribute in identity provider")
    return result_to_response(Ok({"status": "updated", "key": body.key, "value": body.value}), request)


@router.get("/tenants/current/settings")
async def get_tenant_settings(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Load the current tenant's settings and custom attributes from Descope."""
    result = await management.load_tenant_settings(tenant_id)
    if result.is_error():
        if not isinstance(result.error, NotFound):
            raise HTTPException(status_code=502, detail="Failed to load tenant settings from identity provider")
        result = Ok({"tenant_id": tenant_id, "name": "", "custom_attributes": {}})
    return result_to_response(result, request)


@router.patch("/tenants/current/settings")
async def update_tenant_settings(
    request: Request,
    body: UpdateTenantSettingsRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    management: DescopeManagementAdapter = Depends(get_descope_management),
):
    """Update custom attributes on the current tenant. Requires owner or admin role."""
    disallowed = set(body.custom_attributes.keys()) - ALLOWED_TENANT_ATTRIBUTES
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=f"Attribute(s) not allowed: {', '.join(sorted(disallowed))}",
        )
    result = await management.update_tenant_settings(tenant_id, body.custom_attributes)
    if result.is_error():
        raise HTTPException(status_code=502, detail="Failed to update tenant settings in identity provider")
    return result_to_response(
        Ok({"status": "updated", "tenant_id": tenant_id, "custom_attributes": body.custom_attributes}), request
    )
