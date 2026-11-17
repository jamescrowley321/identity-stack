"""TenantRepository — data access layer for canonical Tenant model.

Handles all SQLAlchemy queries for tenant CRUD and user-role lookups.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import User
from app.repositories.base import BaseRepository


class TenantRepository(BaseRepository[Tenant]):
    """Repository for Tenant table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    _model = Tenant

    async def get_by_name(self, name: str) -> Tenant | None:
        """Fetch a tenant by name. Returns None if not found."""
        stmt = sa.select(Tenant).where(Tenant.name == name)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_external_org_id(self, provider_id: uuid.UUID, external_org_id: str) -> list[Tenant]:
        """Every tenant linked to the organization ``external_org_id`` of the provider ``provider_id``."""
        stmt = sa.select(Tenant).where(
            Tenant.external_org_provider_id == provider_id, Tenant.external_org_id == external_org_id
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def external_org_ids_of_user(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> frozenset[str]:
        """The provider's organization ids of the tenants the user holds a role in."""
        stmt = (
            sa.select(Tenant.external_org_id)
            .join(UserTenantRole, UserTenantRole.tenant_id == Tenant.id)
            .where(
                UserTenantRole.user_id == user_id,
                Tenant.external_org_provider_id == provider_id,
                Tenant.external_org_id.is_not(None),
            )
            .distinct()
        )
        result = await self._session.execute(stmt)
        return frozenset(result.scalars().all())

    async def list_all(self) -> list[Tenant]:
        """List all tenants ordered by creation time."""
        stmt = sa.select(Tenant).order_by(Tenant.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

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
