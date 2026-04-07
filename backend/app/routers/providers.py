import uuid

from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies.identity import get_provider_service
from app.dependencies.rbac import require_role
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.models.identity.provider import ProviderType
from app.services.provider import ProviderService

router = APIRouter(tags=["Providers"])


class RegisterProviderRequest(BaseModel):
    name: str = Field(min_length=1)
    type: ProviderType
    issuer_url: str = ""
    base_url: str = ""
    capabilities: list[str] = Field(default_factory=list)
    config_ref: str = ""


class DeactivateProviderRequest(BaseModel):
    active: bool = False


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    """Parse a string to UUID, raising 422 on invalid input."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid UUID for {field_name}: {value}")


def _strip_config_ref(d: dict) -> dict:
    """Remove config_ref from a provider dict before returning to the client."""
    d.pop("config_ref", None)
    return d


@router.get("/providers")
async def list_providers(
    request: Request,
    _operator_roles: list[str] = Depends(require_role("operator")),
    provider_service: ProviderService = Depends(get_provider_service),
):
    """List all registered providers (config_ref excluded)."""
    result = await provider_service.list_providers()
    return result_to_response(result, request)


@router.post("/providers")
@limiter.limit(RATE_LIMIT_AUTH)
async def register_provider(
    request: Request,
    body: RegisterProviderRequest,
    _operator_roles: list[str] = Depends(require_role("operator")),
    provider_service: ProviderService = Depends(get_provider_service),
):
    """Register a new identity provider."""
    result = await provider_service.register_provider(
        name=body.name,
        type=body.type,
        issuer_url=body.issuer_url,
        base_url=body.base_url,
        capabilities=body.capabilities,
        config_ref=body.config_ref,
    )
    if result.is_ok():
        result = Ok(_strip_config_ref(result.ok))
    return result_to_response(result, request, status=201)


@router.patch("/providers/{provider_id}")
@limiter.limit(RATE_LIMIT_AUTH)
async def deactivate_provider(
    request: Request,
    provider_id: str,
    body: DeactivateProviderRequest,
    _operator_roles: list[str] = Depends(require_role("operator")),
    provider_service: ProviderService = Depends(get_provider_service),
):
    """Deactivate a provider (set active=false)."""
    provider_uuid = _parse_uuid(provider_id, "provider_id")
    if body.active is not False:
        raise HTTPException(status_code=422, detail="Only deactivation (active=false) is supported")
    result = await provider_service.deactivate_provider(provider_id=provider_uuid)
    if result.is_ok():
        result = Ok(_strip_config_ref(result.ok))
    return result_to_response(result, request)


@router.get("/providers/{provider_id}/capabilities")
async def get_provider_capabilities(
    request: Request,
    provider_id: str,
    _operator_roles: list[str] = Depends(require_role("operator")),
    provider_service: ProviderService = Depends(get_provider_service),
):
    """Get capabilities for a provider."""
    provider_uuid = _parse_uuid(provider_id, "provider_id")
    result = await provider_service.get_provider_capabilities(provider_id=provider_uuid)
    return result_to_response(result, request)
