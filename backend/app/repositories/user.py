"""UserRepository — data access layer for canonical User model.

Handles all SQLAlchemy queries for user CRUD and search.
Contains NO business logic, NO OTel spans, NO adapter calls — data access only.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity.assignment import UserTenantRole
from app.models.identity.user import User, UserStatus


class RepositoryConflictError(Exception):
    """Raised when a database constraint violation indicates a conflict."""


def _escape_like(value: str) -> str:
    """Escape SQL LIKE/ILIKE wildcard characters."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class UserRepository:
    """Repository for User table operations.

    Takes AsyncSession via constructor injection (inner layer of onion architecture).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user: User) -> User:
        """Add a new user to the session and flush to generate defaults.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        self._session.add(user)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RepositoryConflictError(str(exc)) from exc
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        """Fetch a user by primary key. Returns None if not found."""
        return await self._session.get(User, user_id)

    async def get_for_tenant(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> User | None:
        """Fetch a user by ID, scoped to a tenant via UserTenantRole.

        Returns None if the user does not exist or has no role in the tenant.
        """
        stmt = (
            sa.select(User)
            .join(UserTenantRole, UserTenantRole.user_id == User.id)
            .where(User.id == user_id, UserTenantRole.tenant_id == tenant_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by email address. Returns None if not found."""
        stmt = sa.select(User).where(User.email == email)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update(self, user: User) -> User:
        """Flush updated user state.

        Raises RepositoryConflictError if a uniqueness constraint is violated.
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RepositoryConflictError(str(exc)) from exc
        return user

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

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        await self._session.rollback()
