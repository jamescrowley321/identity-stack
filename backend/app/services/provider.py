"""ProviderService — domain orchestration for canonical Provider operations.

Middle layer of onion architecture: orchestrates ProviderRepository (data access).
All methods return Result[T, IdentityError]. OTel spans on every method.
No credentials stored in Postgres — config_ref points to external secret manager.
"""

from __future__ import annotations

import logging
import uuid

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, IdentityError, NotFound
from app.models.identity.provider import Provider, ProviderType
from app.repositories.provider import ProviderRepository
from app.repositories.user import RepositoryConflictError

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ProviderService:
    """Domain service for Provider operations.

    Orchestrates ProviderRepository (inner).
    Contains NO direct SQLAlchemy imports — uses repository methods only.
    """

    def __init__(
        self,
        *,
        repository: ProviderRepository,
    ) -> None:
        self._repository = repository

    async def register_provider(
        self,
        *,
        name: str,
        type: ProviderType,
        issuer_url: str = "",
        base_url: str = "",
        capabilities: list[str] | None = None,
        config_ref: str = "",
    ) -> Result[dict, IdentityError]:
        """Register a new identity provider.

        AC-4.1.2: no credentials in Postgres, config_ref only.
        """
        with tracer.start_as_current_span("ProviderService.register_provider") as span:
            span.set_attribute("provider.name", name)
            span.set_attribute("provider.type", type.value)

            existing = await self._repository.get_by_name(name)
            if existing is not None:
                return Error(Conflict(message=f"Provider '{name}' already exists"))

            provider = Provider(
                name=name,
                type=type,
                issuer_url=issuer_url,
                base_url=base_url,
                capabilities=capabilities or [],
                config_ref=config_ref,
            )
            # TOCTOU guard: get_by_name pre-check above provides clean error
            # messages, but a concurrent insert can race past it. The
            # RepositoryConflictError fallback below catches the DB-level
            # unique constraint violation. Both checks are required.
            try:
                provider = await self._repository.create(provider)
            except RepositoryConflictError:
                return Error(Conflict(message=f"Provider '{name}' already exists"))

            result_dict = provider.model_dump()
            await self._repository.commit()

            return Ok(result_dict)

    async def deactivate_provider(
        self,
        *,
        provider_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Deactivate a provider. Idempotent — already-inactive returns Ok.

        AC-4.1.2: sets active=False.
        """
        with tracer.start_as_current_span("ProviderService.deactivate_provider") as span:
            span.set_attribute("provider.id", str(provider_id))

            provider = await self._repository.get(provider_id)
            if provider is None:
                return Error(NotFound(message=f"Provider '{provider_id}' not found"))

            if not provider.active:
                return Ok(provider.model_dump())

            provider.active = False
            try:
                provider = await self._repository.update(provider)
            except RepositoryConflictError:
                return Error(Conflict(message=f"Provider '{provider_id}' conflict during deactivation"))

            result_dict = provider.model_dump()
            await self._repository.commit()

            return Ok(result_dict)

    async def get_provider_capabilities(
        self,
        *,
        provider_id: uuid.UUID,
    ) -> Result[list[str], IdentityError]:
        """Get the capabilities list for a provider.

        AC-4.1.2: returns the capabilities JSON array.
        """
        with tracer.start_as_current_span("ProviderService.get_provider_capabilities") as span:
            span.set_attribute("provider.id", str(provider_id))

            provider = await self._repository.get(provider_id)
            if provider is None:
                return Error(NotFound(message=f"Provider '{provider_id}' not found"))

            return Ok(provider.capabilities)
