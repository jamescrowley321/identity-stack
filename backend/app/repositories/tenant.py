"""TenantRepository — data access layer for canonical Tenant model.

Handles all SQLAlchemy queries for tenant CRUD and user-role lookups.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import User
from app.repositories.user import RepositoryConflictError


class TenantRepository:
    """Repository for Tenant table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, tenant: Tenant) -> Tenant:
        """Add a new tenant to the session and flush to generate defaults.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        self._session.add(tenant)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RepositoryConflictError(str(exc)) from exc
        return tenant

    async def get(self, tenant_id: uuid.UUID) -> Tenant | None:
        """Fetch a tenant by primary key. Returns None if not found."""
        return await self._session.get(Tenant, tenant_id)

    async def get_by_name(self, name: str) -> Tenant | None:
        """Fetch a tenant by name. Returns None if not found."""
        stmt = sa.select(Tenant).where(Tenant.name == name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> list[Tenant]:
        """List all tenants ordered by creation time."""
        stmt = sa.select(Tenant).order_by(Tenant.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update(self, tenant: Tenant) -> Tenant:
        """Flush updated tenant state.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RepositoryConflictError(str(exc)) from exc
        return tenant

    async def get_users_with_roles(self, tenant_id: uuid.UUID) -> list[tuple]:
        """Get all users and their roles for a tenant via 3-way JOIN.

        Returns list of (User, Role) tuples for the given tenant.
        """
        stmt = (
            sa.select(User, Role)
            .join(UserTenantRole, UserTenantRole.user_id == User.id)
            .join(Role, Role.id == UserTenantRole.role_id)
            .where(UserTenantRole.tenant_id == tenant_id)
            .order_by(User.email, Role.name)
        )
        result = await self._session.execute(stmt)
        return list(result.all())

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self._session.rollback()
