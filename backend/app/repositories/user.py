"""UserRepository — data access layer for canonical User model.

Handles all SQLAlchemy queries for user CRUD and search.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.models.identity.assignment import UserTenantRole
from app.models.identity.user import User, UserStatus
from app.repositories.base import BaseRepository


def _escape_like(value: str) -> str:
    """Escape SQL LIKE/ILIKE wildcard characters."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class UserRepository(BaseRepository[User]):
    """Repository for User table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    _model = User

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by email address. Returns None if not found."""
        stmt = sa.select(User).where(User.email == email)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        email: str | None = None,
        name: str | None = None,
        status: UserStatus | None = None,
    ) -> list[User]:
        """Search users scoped to a tenant with optional filters.

        Tenant scoping uses a JOIN through user_tenant_roles since users
        don't have a direct tenant_id column.
        """
        stmt = (
            sa.select(User)
            .join(UserTenantRole, UserTenantRole.user_id == User.id)
            .where(UserTenantRole.tenant_id == tenant_id)
            .distinct()
        )

        if email is not None:
            escaped = _escape_like(email)
            stmt = stmt.where(User.email.ilike(f"%{escaped}%", escape="\\"))

        if name is not None:
            escaped = _escape_like(name)
            stmt = stmt.where(
                sa.or_(
                    User.given_name.ilike(f"%{escaped}%", escape="\\"),
                    User.family_name.ilike(f"%{escaped}%", escape="\\"),
                    User.user_name.ilike(f"%{escaped}%", escape="\\"),
                )
            )

        if status is not None:
            stmt = stmt.where(User.status == status)

        stmt = stmt.order_by(User.created_at.desc())

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_all(self) -> list[User]:
        """List all users ordered by creation time."""
        stmt = sa.select(User).order_by(User.created_at.desc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def exists_in_tenant(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
        """Check if a user has any role assignment in the given tenant."""
        stmt = sa.select(
            sa.exists().where(
                UserTenantRole.user_id == user_id,
                UserTenantRole.tenant_id == tenant_id,
            )
        )
        result = await self._session.execute(stmt)
        return bool(result.scalar())
