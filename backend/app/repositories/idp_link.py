"""IdPLinkRepository — data access layer for canonical IdPLink model.

Handles all SQLAlchemy queries for IdP link CRUD.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity.provider import Provider
from app.models.identity.user import IdPLink
from app.repositories.user import RepositoryConflictError


class IdPLinkRepository:
    """Repository for IdPLink table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, link_id: uuid.UUID) -> IdPLink | None:
        """Fetch an IdP link by primary key. Returns None if not found."""
        return await self._session.get(IdPLink, link_id)

    async def delete(self, link_id: uuid.UUID) -> bool:
        """Delete an IdP link by primary key. Returns True if deleted, False if not found."""
        link = await self._session.get(IdPLink, link_id)
        if link is None:
            return False
        await self._session.delete(link)
        await self._session.flush()
        return True

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
        Does NOT rollback — the caller (service layer) owns the transaction.
        """
        self._session.add(link)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return link

    async def get_by_provider_name_and_sub(self, provider_name: str, external_sub: str) -> IdPLink | None:
        """Fetch an IdP link by provider name and external subject (joins Provider table).

        Returns None if no matching link is found.
        """
        stmt = (
            sa.select(IdPLink)
            .join(Provider, Provider.id == IdPLink.provider_id)
            .where(Provider.name == provider_name, IdPLink.external_sub == external_sub)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

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
