import uuid

from expression import Error, Ok
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.identity import get_idp_link_service
from app.dependencies.rbac import require_role
from app.errors.identity import NotFound
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.models.database import get_async_session
from app.repositories.assignment import UserTenantRoleRepository
from app.services.idp_link import IdPLinkService

router = APIRouter(tags=["IdP Links"])


class CreateIdPLinkRequest(BaseModel):
    provider_id: uuid.UUID
    # OIDC `sub` has no spec max; 255 is the practical limit used by most IdPs
    external_sub: str = Field(min_length=1, max_length=255)
    # RFC 5321 §4.5.3.1.3: 64 local-part + @ + 255 domain = 320 max
    external_email: str = Field(default="", max_length=320)
    metadata: dict[str, str] | None = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata_size(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        # 20 keys is generous for any real use case
        if v is not None and len(v) > 20:
            raise ValueError("metadata must have at most 20 keys")
        return v


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    """Parse a string to UUID, raising 422 on invalid input."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid UUID for {field_name}: {value}")


_NOT_IN_TENANT = "User not found in tenant"


async def _verify_user_in_tenant(
    user_uuid: uuid.UUID,
    request: Request,
    session: AsyncSession,
) -> Response | None:
    """Verify the target user belongs to the caller's tenant.

    Returns an RFC 9457 Problem Detail 404 response when they do not, and None
    when the check passes. 404 rather than 403 so the reply does not leak whether
    the user exists. Callers must return a non-None result unchanged; the shape
    matches every other error this router emits via ``result_to_response``,
    rather than the bare ``{"detail": ...}`` a raised HTTPException produces.
    """
    claims = getattr(request.state, "claims", None)
    tenant_id = claims.get("dct") if claims else None
    if not tenant_id:
        return result_to_response(Error(NotFound(message=_NOT_IN_TENANT)), request)
    # `dct` is the IdP's own opaque tenant id — a Descope tenant id looks like
    # "T3Bj8QOcyflY8V0bvSu1eEoHmjk6" — while user_tenant_roles.tenant_id is a
    # canonical UUID foreign key. A `dct` that is not a UUID therefore cannot
    # match any assignment row, which is a 404 (this user is not in the caller's
    # tenant), not a server error. Coercing it unguarded raised ValueError out of
    # the handler and surfaced to the client as a 500.
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except (AttributeError, TypeError, ValueError):
        return result_to_response(Error(NotFound(message=_NOT_IN_TENANT)), request)
    assignment_repo = UserTenantRoleRepository(session)
    assignments = await assignment_repo.list_by_user_tenant(user_uuid, tenant_uuid)
    if not assignments:
        return result_to_response(Error(NotFound(message=_NOT_IN_TENANT)), request)
    return None


@router.get("/users/{user_id}/idp-links")
async def list_user_idp_links(
    request: Request,
    user_id: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """List all IdP links for a user."""
    user_uuid = _parse_uuid(user_id, "user_id")
    if (denied := await _verify_user_in_tenant(user_uuid, request, session)) is not None:
        return denied
    result = await idp_link_service.get_user_idp_links(user_id=user_uuid)
    if result.is_ok():
        result = Ok({"idp_links": result.ok})
    return result_to_response(result, request)


@router.post("/users/{user_id}/idp-links")
@limiter.limit(RATE_LIMIT_AUTH)
async def create_idp_link(
    request: Request,
    user_id: str,
    body: CreateIdPLinkRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """Create an IdP link between a user and an external identity."""
    user_uuid = _parse_uuid(user_id, "user_id")
    if (denied := await _verify_user_in_tenant(user_uuid, request, session)) is not None:
        return denied
    result = await idp_link_service.create_idp_link(
        user_id=user_uuid,
        provider_id=body.provider_id,
        external_sub=body.external_sub,
        external_email=body.external_email,
        metadata=body.metadata,
    )
    return result_to_response(result, request, status=201)


@router.delete("/users/{user_id}/idp-links/{link_id}")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_idp_link(
    request: Request,
    user_id: str,
    link_id: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """Delete an IdP link."""
    user_uuid = _parse_uuid(user_id, "user_id")
    if (denied := await _verify_user_in_tenant(user_uuid, request, session)) is not None:
        return denied
    link_uuid = _parse_uuid(link_id, "link_id")
    result = await idp_link_service.delete_idp_link(link_id=link_uuid, user_id=user_uuid)
    if result.is_ok():
        return Response(status_code=204)
    return result_to_response(result, request)
