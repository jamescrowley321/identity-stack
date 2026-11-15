"""RoleRepository — data access layer for canonical Role and RolePermission models.

Handles all SQLAlchemy queries for role CRUD and permission mapping, and maps
role rows to and from the Access domain's values.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.domain import access
from app.models.identity.role import Permission, Role, RolePermission
from app.repositories.base import BaseRepository, RepositoryConflictError


def role_from_row(row: Role) -> access.Role:
    """Rebuild the domain value. A stored name is trusted, not re-validated."""
    return access.Role(
        id=row.id,
        name=access.RoleName(row.name),
        description=row.description,
        tenant_id=row.tenant_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def role_to_row(role: access.Role) -> Role:
    """Build the row for a role that has not been stored yet."""
    return Role(
        id=role.id,
        name=role.name.value,
        description=role.description,
        tenant_id=role.tenant_id,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def apply_role(role: access.Role, row: Role) -> None:
    """Copy the fields an update may change onto the stored row.

    ``updated_at`` is left to the database, which stamps it on write.
    """
    row.name = role.name.value
    row.description = role.description


class RoleRepository(BaseRepository[Role]):
    """Repository for Role table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    _model = Role

    async def get_by_name(self, name: str, tenant_id: uuid.UUID | None = None) -> Role | None:
        """Fetch a role by name within a tenant scope. Returns None if not found."""
        stmt = sa.select(Role).where(Role.name == name)
        if tenant_id is not None:
            stmt = stmt.where(Role.tenant_id == tenant_id)
        else:
            stmt = stmt.where(Role.tenant_id.is_(None))
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_tenant(self, tenant_id: uuid.UUID | None = None) -> list[Role]:
        """List roles filtered by tenant_id (None for global roles)."""
        stmt = sa.select(Role)
        if tenant_id is not None:
            stmt = stmt.where(Role.tenant_id == tenant_id)
        else:
            stmt = stmt.where(Role.tenant_id.is_(None))
        stmt = stmt.order_by(Role.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def add_permission(self, role_id: uuid.UUID, permission_id: uuid.UUID) -> RolePermission:
        """Create a role-permission mapping.

        Raises RepositoryConflictError if the mapping already exists.
        """
        mapping = RolePermission(role_id=role_id, permission_id=permission_id)
        self._session.add(mapping)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return mapping

    async def remove_permission(self, role_id: uuid.UUID, permission_id: uuid.UUID) -> bool:
        """Remove a role-permission mapping. Returns True if deleted, False if not found."""
        stmt = sa.delete(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == permission_id,
        )
        result = await self._session.execute(stmt)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc
        return result.rowcount > 0

    async def get_permissions(self, role_id: uuid.UUID) -> list[Permission]:
        """Get all permissions mapped to a role via role_permissions join."""
        stmt = (
            sa.select(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
            .order_by(Permission.name)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
