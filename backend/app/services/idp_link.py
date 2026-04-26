"""IdPLinkService — domain orchestration for canonical IdP link operations.

Middle layer of onion architecture: orchestrates IdPLinkRepository, UserRepository,
and ProviderRepository (data access). All methods return Result[T, IdentityError].
OTel spans on every method.
"""

from __future__ import annotations

import logging
import uuid

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, IdentityError, NotFound
from app.models.identity.user import IdPLink
from app.repositories.base import RepositoryConflictError
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.user import UserRepository

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class IdPLinkService:
    """Domain service for IdP link operations.

    Orchestrates IdPLinkRepository, UserRepository, and ProviderRepository (inner).
    Contains NO direct SQLAlchemy imports — uses repository methods only.
    """

    def __init__(
        self,
        *,
        repository: IdPLinkRepository,
        user_repository: UserRepository,
        provider_repository: ProviderRepository,
    ) -> None:
        self._repository = repository
        self._user_repository = user_repository
        self._provider_repository = provider_repository

    async def create_idp_link(
        self,
        *,
        user_id: uuid.UUID,
        provider_id: uuid.UUID,
        external_sub: str,
        external_email: str = "",
        metadata: dict | None = None,
    ) -> Result[dict, IdentityError]:
        """Create a new IdP link between a user and an external identity.

        AC-4.1.1: validates user and provider exist, enforces unique constraints.
        """
        with tracer.start_as_current_span("IdPLinkService.create_idp_link") as span:
            span.set_attribute("user.id", str(user_id))
            span.set_attribute("provider.id", str(provider_id))

            user = await self._user_repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

            provider = await self._provider_repository.get(provider_id)
            if provider is None:
                return Error(NotFound(message=f"Provider '{provider_id}' not found"))

            link = IdPLink(
                user_id=user_id,
                provider_id=provider_id,
                external_sub=external_sub,
                external_email=external_email,
                metadata_=metadata,
            )
            try:
                link = await self._repository.create(link)
            except RepositoryConflictError:
                msg = f"IdP link already exists for user '{user_id}' with provider '{provider_id}'"
                return Error(Conflict(message=msg))

            result_dict = link.model_dump()
            await self._repository.commit()

            return Ok(result_dict)

    async def get_user_idp_links(
        self,
        *,
        user_id: uuid.UUID,
    ) -> Result[list[dict], IdentityError]:
        """Retrieve all IdP links for a user.

        AC-4.1.1: delegates to IdPLinkRepository.get_by_user.
        """
        with tracer.start_as_current_span("IdPLinkService.get_user_idp_links") as span:
            span.set_attribute("user.id", str(user_id))

            links = await self._repository.get_by_user(user_id)
            return Ok([link.model_dump() for link in links])

    async def delete_idp_link(
        self,
        *,
        link_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Delete an IdP link by ID, scoped to the owning user.

        AC-4.1.1: returns NotFound if link does not exist or belongs to another user.
        """
        with tracer.start_as_current_span("IdPLinkService.delete_idp_link") as span:
            span.set_attribute("link.id", str(link_id))
            span.set_attribute("user.id", str(user_id))

            link = await self._repository.get(link_id)
            if link is None or link.user_id != user_id:
                return Error(NotFound(message=f"IdP link '{link_id}' not found for user '{user_id}'"))

            await self._repository.delete(link_id)
            await self._repository.commit()

            return Ok({"status": "deleted", "link_id": str(link_id)})
