"""BaseRepository — generic base class for shared CRUD and transaction operations.

Consolidates duplicated __init__, create, get, update, delete, commit, rollback
methods that every concrete repository previously implemented individually.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


class RepositoryConflictError(Exception):
    """Raised when a database constraint violation indicates a conflict."""


class BaseRepository[T]:
    """Base repository with shared CRUD and transaction operations.

    Subclasses set ``_model`` class attribute to their SQLAlchemy model.
    """

    _model: type[T]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, entity: T) -> T:
        """Add a new entity to the session and flush to generate defaults.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        Does NOT rollback — the service layer owns the transaction boundary.
        """
        self._session.add(entity)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return entity

    async def get(self, entity_id: uuid.UUID) -> T | None:
        """Fetch an entity by primary key. Returns None if not found."""
        return await self._session.get(self._model, entity_id)

    async def update(self, entity: T) -> T:
        """Flush updated entity state.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        Does NOT rollback — the service layer owns the transaction boundary.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return entity

    async def delete(self, entity_id: uuid.UUID) -> bool:
        """Delete an entity by ID. Returns True if deleted, False if not found."""
        stmt = sa.delete(self._model).where(self._model.id == entity_id)
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self._session.rollback()
