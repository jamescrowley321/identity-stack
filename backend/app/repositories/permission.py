"""PermissionRepository — data access layer for canonical Permission model.

Handles all SQLAlchemy queries for permission CRUD, and maps permission rows to
and from the Access domain's values.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.domain import access
from app.models.identity.role import Permission
from app.repositories.base import BaseRepository


def permission_from_row(row: Permission) -> access.Permission:
    """Rebuild the domain value. A stored name is trusted, not re-validated."""
    return access.Permission(
        id=row.id,
        name=access.PermissionName(row.name),
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def permission_to_row(permission: access.Permission) -> Permission:
    """Build the row for a permission that has not been stored yet."""
    return Permission(
        id=permission.id,
        name=permission.name.value,
        description=permission.description,
        created_at=permission.created_at,
        updated_at=permission.updated_at,
    )


def apply_permission(permission: access.Permission, row: Permission) -> None:
    """Copy the fields an update may change onto the stored row.

    ``updated_at`` is left to the database, which stamps it on write.
    """
    row.name = permission.name.value
    row.description = permission.description


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
