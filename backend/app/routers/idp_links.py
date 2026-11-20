import uuid

from expression import Ok
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.identity import get_idp_link_service
from app.dependencies.rbac import (
    OPERATOR_ROLE,
    refuse_machine_principal,
    require_canonical_principal,
    require_global_role,
)
from app.errors.identity import IdentityErrorRaised, NotFound
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.models.database import get_async_session
from app.repositories.user import UserRepository
from app.services.idp_link import IdPLinkService

router = APIRouter(tags=["IdP Links"])
# A link decides which provider account signs in as a person, so no machine credential makes or removes one.
# Only the canonical model's grants open a link: a Descope principal's claims, operator among them, do not (#722).
CANONICAL_ONLY = [Depends(require_canonical_principal)]
NO_MACHINES = [*CANONICAL_ONLY, Depends(refuse_machine_principal)]
# A link signs its login in as the person in every tenant they belong to, with every grant they hold,
# so it is state that belongs to no tenant: an operator manages it, never a tenant's admin (#722).
LINK_MANAGERS = require_global_role(OPERATOR_ROLE)


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


_USER_NOT_FOUND = "User not found"


async def _require_user(user_uuid: uuid.UUID, session: AsyncSession) -> None:
    """Refuse (404) a user the canonical model does not hold, before any link of theirs is read or changed.

    Raises rather than returning a response, so a route that calls it cannot drop the refusal.
    """
    if await UserRepository(session).get(user_uuid) is None:
        raise IdentityErrorRaised(NotFound(message=_USER_NOT_FOUND))


@router.get("/users/{user_id}/idp-links", dependencies=CANONICAL_ONLY)
async def list_user_idp_links(
    request: Request,
    user_id: str,
    _operator_roles: list[str] = Depends(LINK_MANAGERS),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """List all IdP links for a user."""
    user_uuid = _parse_uuid(user_id, "user_id")
    await _require_user(user_uuid, session)
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
    _operator_roles: list[str] = Depends(LINK_MANAGERS),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """Create an IdP link between a user and an external identity."""
    user_uuid = _parse_uuid(user_id, "user_id")
    await _require_user(user_uuid, session)
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
    _operator_roles: list[str] = Depends(LINK_MANAGERS),
    idp_link_service: IdPLinkService = Depends(get_idp_link_service),
    session: AsyncSession = Depends(get_async_session),
):
    """Delete an IdP link."""
    user_uuid = _parse_uuid(user_id, "user_id")
    await _require_user(user_uuid, session)
    link_uuid = _parse_uuid(link_id, "link_id")
    result = await idp_link_service.delete_idp_link(link_id=link_uuid, user_id=user_uuid)
    if result.is_ok():
        return Response(status_code=204)
    return result_to_response(result, request)
