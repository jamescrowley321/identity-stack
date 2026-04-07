"""ProviderRepository — data access layer for canonical Provider model.

Handles all SQLAlchemy queries for provider CRUD.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity.provider import Provider, ProviderType
from app.repositories.user import RepositoryConflictError


class ProviderRepository:
    """Repository for Provider table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, provider: Provider) -> Provider:
        """Add a new provider to the session and flush to generate defaults.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        Does NOT rollback — the caller (service layer) owns the transaction.
        """
        self._session.add(provider)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return provider

    async def update(self, provider: Provider) -> Provider:
        """Flush pending mutations on a provider already attached to the session.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return provider

    async def get_by_type(self, provider_type: ProviderType) -> Provider | None:
        """Fetch a provider by type. Returns the first match or None.

        Uses scalars().first() rather than scalar_one_or_none() to avoid
        MultipleResultsFound if duplicate providers exist for a type.
        """
        stmt = sa.select(Provider).where(Provider.type == provider_type)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def get_by_name(self, name: str) -> Provider | None:
        """Fetch a provider by name. Returns None if not found."""
        stmt = sa.select(Provider).where(Provider.name == name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get(self, provider_id: uuid.UUID) -> Provider | None:
        """Fetch a provider by primary key. Returns None if not found."""
        return await self._session.get(Provider, provider_id)

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()
