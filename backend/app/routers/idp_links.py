import uuid

from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.identity import get_idp_link_service
from app.dependencies.rbac import refuse_machine_principal, require_role
from app.dependencies.tenant import get_canonical_tenant_id
from app.errors.identity import IdentityErrorRaised, NotFound
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.models.database import get_async_session
from app.repositories.assignment import UserTenantRoleRepository
from app.services.idp_link import IdPLinkService

router = APIRouter(tags=["IdP Links"])
# A link decides which provider account signs in as a person, so no machine credential makes or removes one.
NO_MACHINES = [Depends(refuse_machine_principal)]


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


async def _require_user_in_tenant(
    user_uuid: uuid.UUID,
    tenant_id: str | None,
    session: AsyncSession,
) -> None:
    """Verify the target user belongs to the caller's canonical tenant, or refuse the request.

    ``tenant_id`` is ``get_canonical_tenant_id``'s answer: the tenant resolver's
    tenant for a principal authorized from the canonical model, ``None`` for any
    other. A Descope principal's ``dct`` is Descope's own tenant id, which names
    no canonical tenant even when it is spelled as one of their UUIDs, so it is
    refused here and never compared with ``user_tenant_roles`` (#722).

    Raises ``IdentityErrorRaised(NotFound)`` when the user is not in the tenant,
    or the caller has no canonical tenant: 404 rather than 403, so the reply does
    not leak whether the user exists. It raises rather than returning a response,
    so a route that calls it cannot drop the refusal.
    """
    if tenant_id is None:
        raise IdentityErrorRaised(NotFound(message=_NOT_IN_TENANT))
    assignment_repo = UserTenantRoleRepository(session)
    assignments = await assignment_repo.list_by_user_tenant(user_uuid, uuid.UUID(tenant_id))
    if not assignments:
        raise IdentityErrorRaised(NotFound(message=_NOT_IN_TENANT))


@router.get("/users/{user_id}/idp-links")
async def list_user_idp_links(
    request: Request,
    user_id: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    tenant_id: str | None = Depends(get_canonical_tenant_id),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """List all IdP links for a user."""
    user_uuid = _parse_uuid(user_id, "user_id")
    await _require_user_in_tenant(user_uuid, tenant_id, session)
    result = await idp_link_service.get_user_idp_links(user_id=user_uuid)
    if result.is_ok():
        result = Ok({"idp_links": result.ok})
    return result_to_response(result, request)


@router.post("/users/{user_id}/idp-links", dependencies=NO_MACHINES)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_idp_link(
    request: Request,
    user_id: str,
    body: CreateIdPLinkRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    tenant_id: str | None = Depends(get_canonical_tenant_id),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """Create an IdP link between a user and an external identity."""
    user_uuid = _parse_uuid(user_id, "user_id")
    await _require_user_in_tenant(user_uuid, tenant_id, session)
    result = await idp_link_service.create_idp_link(
        user_id=user_uuid,
        provider_id=body.provider_id,
        external_sub=body.external_sub,
        external_email=body.external_email,
        metadata=body.metadata,
    )
    return result_to_response(result, request, status=201)


@router.delete("/users/{user_id}/idp-links/{link_id}", dependencies=NO_MACHINES)
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_idp_link(
    request: Request,
    user_id: str,
    link_id: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    tenant_id: str | None = Depends(get_canonical_tenant_id),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """Delete an IdP link."""
    user_uuid = _parse_uuid(user_id, "user_id")
    await _require_user_in_tenant(user_uuid, tenant_id, session)
    link_uuid = _parse_uuid(link_id, "link_id")
    result = await idp_link_service.delete_idp_link(link_id=link_uuid, user_id=user_uuid)
    if result.is_ok():
        return Response(status_code=204)
    return result_to_response(result, request)
