import uuid

from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.identity import get_idp_link_service
from app.dependencies.rbac import require_role
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.models.database import get_async_session
from app.repositories.assignment import UserTenantRoleRepository
from app.services.idp_link import IdPLinkService

router = APIRouter(tags=["IdP Links"])


class CreateIdPLinkRequest(BaseModel):
    provider_id: uuid.UUID
    external_sub: str = Field(min_length=1, max_length=255)
    external_email: str = Field(default="", max_length=320)
    metadata: dict[str, str] | None = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata_size(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is not None and len(v) > 20:
            raise ValueError("metadata must have at most 20 keys")
        return v


def _parse_uuid(value: str, field_name: str) -> uuid.UUID:
    """Parse a string to UUID, raising 422 on invalid input."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid UUID for {field_name}: {value}")


async def _verify_user_in_tenant(
    user_uuid: uuid.UUID,
    request: Request,
    session: AsyncSession,
) -> None:
    """Verify the target user belongs to the caller's tenant.

    Returns 404 (not 403) to avoid leaking whether the user exists.
    """
    claims = getattr(request.state, "claims", None)
    tenant_id = claims.get("dct") if claims else None
    if not tenant_id:
        raise HTTPException(status_code=404, detail="User not found in tenant")
    assignment_repo = UserTenantRoleRepository(session)
    assignments = await assignment_repo.list_by_user_tenant(user_uuid, uuid.UUID(tenant_id))
    if not assignments:
        raise HTTPException(status_code=404, detail="User not found in tenant")


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
    await _verify_user_in_tenant(user_uuid, request, session)
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
    await _verify_user_in_tenant(user_uuid, request, session)
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
    await _verify_user_in_tenant(user_uuid, request, session)
    link_uuid = _parse_uuid(link_id, "link_id")
    result = await idp_link_service.delete_idp_link(link_id=link_uuid, user_id=user_uuid)
    if result.is_ok():
        return Response(status_code=204)
    return result_to_response(result, request)
