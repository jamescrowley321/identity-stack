"""UserTenantRoleRepository — data access layer for role assignment model.

Handles all SQLAlchemy queries for user-tenant-role assignment CRUD, and maps
assignment rows to and from the Access domain's values.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.domain import access
from app.models.identity.assignment import UserTenantRole
from app.repositories.base import BaseRepository


def assignment_from_row(row: UserTenantRole) -> access.Assignment:
    return access.Assignment(
        user_id=row.user_id,
        tenant_id=row.tenant_id,
        role_id=row.role_id,
        assigned_by=row.assigned_by,
        assigned_at=row.assigned_at,
    )


def assignment_to_row(assignment: access.Assignment) -> UserTenantRole:
    """Build the row for an assignment that has not been stored yet."""
    return UserTenantRole(
        user_id=assignment.user_id,
        tenant_id=assignment.tenant_id,
        role_id=assignment.role_id,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.assigned_at,
    )


class UserTenantRoleRepository(BaseRepository[UserTenantRole]):
    """Repository for UserTenantRole table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    _model = UserTenantRole

    async def get(self, user_id: uuid.UUID, tenant_id: uuid.UUID, role_id: uuid.UUID) -> UserTenantRole | None:
        """Fetch an assignment by composite primary key. Returns None if not found."""
        return await self._session.get(UserTenantRole, (user_id, tenant_id, role_id))

    async def list_by_user_tenant(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> list[UserTenantRole]:
        """List all role assignments for a user within a tenant."""
        stmt = (
            sa.select(UserTenantRole)
            .where(
                UserTenantRole.user_id == user_id,
                UserTenantRole.tenant_id == tenant_id,
            )
            .order_by(UserTenantRole.assigned_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_user(self, user_id: uuid.UUID) -> list[UserTenantRole]:
        """List all role assignments for a user across all tenants."""
        stmt = (
            sa.select(UserTenantRole)
            .where(UserTenantRole.user_id == user_id)
            .order_by(UserTenantRole.tenant_id, UserTenantRole.assigned_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_role(self, role_id: uuid.UUID) -> list[UserTenantRole]:
        """List every assignment of a role, across users and tenants."""
        stmt = (
            sa.select(UserTenantRole)
            .where(UserTenantRole.role_id == role_id)
            .order_by(UserTenantRole.tenant_id, UserTenantRole.user_id)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def delete(self, user_id: uuid.UUID, tenant_id: uuid.UUID, role_id: uuid.UUID) -> bool:
        """Delete a role assignment. Returns True if deleted, False if not found."""
        stmt = sa.delete(UserTenantRole).where(
            UserTenantRole.user_id == user_id,
            UserTenantRole.tenant_id == tenant_id,
            UserTenantRole.role_id == role_id,
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount > 0

    async def delete_by_user_tenant(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> int:
        """Delete all role assignments for a user in a tenant. Returns count of deleted rows."""
        stmt = sa.delete(UserTenantRole).where(
            UserTenantRole.user_id == user_id,
            UserTenantRole.tenant_id == tenant_id,
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.rowcount
