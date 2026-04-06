"""PermissionRepository — data access layer for canonical Permission model.

Handles all SQLAlchemy queries for permission CRUD.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity.role import Permission
from app.repositories.user import RepositoryConflictError


class PermissionRepository:
    """Repository for Permission table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, permission: Permission) -> Permission:
        """Add a new permission to the session and flush to generate defaults.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        self._session.add(permission)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return permission

    async def get(self, permission_id: uuid.UUID) -> Permission | None:
        """Fetch a permission by primary key. Returns None if not found."""
        return await self._session.get(Permission, permission_id)

    async def get_by_name(self, name: str) -> Permission | None:
        """Fetch a permission by name. Returns None if not found."""
        stmt = sa.select(Permission).where(Permission.name == name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Permission]:
        """List all permissions ordered by creation time."""
        stmt = sa.select(Permission).order_by(Permission.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update(self, permission: Permission) -> Permission:
        """Flush updated permission state.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return permission

    async def delete(self, permission_id: uuid.UUID) -> bool:
        """Delete a permission by ID. Returns True if deleted, False if not found."""
        stmt = sa.delete(Permission).where(Permission.id == permission_id)
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self._session.rollback()
