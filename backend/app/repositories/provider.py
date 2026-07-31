"""ProviderRepository — data access layer for canonical Provider model.

Handles all SQLAlchemy queries for provider CRUD.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.identity.provider import Provider, ProviderType
from app.repositories.base import BaseRepository


class ProviderRepository(BaseRepository[Provider]):
    """Repository for Provider table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    _model = Provider

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

    async def list_all(self) -> list[Provider]:
        """Return all providers ordered by name."""
        stmt = sa.select(Provider).order_by(Provider.name)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
