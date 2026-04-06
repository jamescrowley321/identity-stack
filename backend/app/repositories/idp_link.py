"""IdPLinkRepository — data access layer for canonical IdPLink model.

Handles all SQLAlchemy queries for IdP link CRUD.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity.user import IdPLink
from app.repositories.user import RepositoryConflictError


class IdPLinkRepository:
    """Repository for IdPLink table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_provider_and_sub(self, provider_id: uuid.UUID, external_sub: str) -> IdPLink | None:
        """Fetch an IdP link by provider and external subject. Returns None if not found."""
        stmt = sa.select(IdPLink).where(
            IdPLink.provider_id == provider_id,
            IdPLink.external_sub == external_sub,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, link: IdPLink) -> IdPLink:
        """Add a new IdP link to the session and flush to generate defaults.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        self._session.add(link)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RepositoryConflictError(str(exc)) from exc
        return link

    async def get_by_user(self, user_id: uuid.UUID) -> list[IdPLink]:
        """Fetch all IdP links for a user."""
        stmt = sa.select(IdPLink).where(IdPLink.user_id == user_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self._session.rollback()
