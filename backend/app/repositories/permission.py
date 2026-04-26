"""PermissionRepository — data access layer for canonical Permission model.

Handles all SQLAlchemy queries for permission CRUD.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.identity.role import Permission
from app.repositories.base import BaseRepository


class PermissionRepository(BaseRepository[Permission]):
    """Repository for Permission table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    _model = Permission

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
