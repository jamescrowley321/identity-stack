"""RoleRepository — data access layer for canonical Role and RolePermission models.

Handles all SQLAlchemy queries for role CRUD and permission mapping.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity.role import Permission, Role, RolePermission
from app.repositories.user import RepositoryConflictError


class RoleRepository:
    """Repository for Role table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, role: Role) -> Role:
        """Add a new role to the session and flush to generate defaults.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        self._session.add(role)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RepositoryConflictError(str(exc)) from exc
        return role

    async def get(self, role_id: uuid.UUID) -> Role | None:
        """Fetch a role by primary key. Returns None if not found."""
        return await self._session.get(Role, role_id)

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

    async def update(self, role: Role) -> Role:
        """Flush updated role state.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RepositoryConflictError(str(exc)) from exc
        return role

    async def add_permission(self, role_id: uuid.UUID, permission_id: uuid.UUID) -> RolePermission:
        """Create a role-permission mapping.

        Raises RepositoryConflictError if the mapping already exists.
        """
        mapping = RolePermission(role_id=role_id, permission_id=permission_id)
        self._session.add(mapping)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RepositoryConflictError(str(exc)) from exc
        return mapping

    async def remove_permission(self, role_id: uuid.UUID, permission_id: uuid.UUID) -> bool:
        """Remove a role-permission mapping. Returns True if deleted, False if not found."""
        stmt = sa.delete(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == permission_id,
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
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

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self._session.rollback()
