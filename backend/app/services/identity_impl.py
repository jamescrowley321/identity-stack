"""PostgresIdentityService — concrete IdentityService backed by Postgres + IdP adapter.

Story 2.1 scope: User CRUD and search. All other methods raise NotImplementedError.
Enforcement: AsyncSession only, Result[T, IdentityError] returns, OTel spans, explicit tenant_id.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from expression import Error, Ok, Result
from opentelemetry import trace
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors.identity import Conflict, IdentityError, NotFound
from app.models.identity.assignment import UserTenantRole
from app.models.identity.user import User, UserStatus
from app.services.adapters.base import IdentityProviderAdapter
from app.services.identity import IdentityService

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class PostgresIdentityService(IdentityService):
    """IdentityService implementation backed by Postgres with write-through sync."""

    def __init__(self, session: AsyncSession, adapter: IdentityProviderAdapter) -> None:
        self._session = session
        self._adapter = adapter

    def _user_to_dict(self, user: User) -> dict:
        """Serialize a User model to a JSON-safe dict."""
        return {
            "id": str(user.id),
            "email": user.email,
            "user_name": user.user_name,
            "given_name": user.given_name,
            "family_name": user.family_name,
            "status": user.status.value if isinstance(user.status, UserStatus) else user.status,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
        }

    # --- User operations (Story 2.1) ---

    async def create_user(
        self,
        *,
        tenant_id: uuid.UUID,
        email: str,
        user_name: str,
        given_name: str = "",
        family_name: str = "",
    ) -> Result[dict, IdentityError]:
        """Create a user record in Postgres and sync to IdP.

        Note: tenant_id is used for OTel tracing. The user-to-tenant association
        (UserTenantRole) is created via assign_role_to_user() because the data model
        requires a role_id as part of the composite PK. Until assigned, the user will
        not appear in search_users() which queries via UserTenantRole join.
        """
        with tracer.start_as_current_span(
            "identity.create_user",
            attributes={"tenant.id": str(tenant_id)},
        ):
            user = User(
                email=email,
                user_name=user_name,
                given_name=given_name,
                family_name=family_name,
                status=UserStatus.active,
            )
            self._session.add(user)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"User with email '{email}' already exists"))

            user_dict = self._user_to_dict(user)

            # Write-through: sync to IdP, log on failure, never rollback (D7)
            sync_result = await self._adapter.sync_user(
                user_id=user.id,
                data=user_dict,
            )
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after create_user: operation=%s user_id=%s payload=%s error=%s",
                    sync_err.operation,
                    user.id,
                    user_dict,
                    sync_err.message,
                )
                return Ok(user_dict)

            return Ok(user_dict)

    async def get_user(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.get_user",
            attributes={"tenant.id": str(tenant_id), "user.id": str(user_id)},
        ):
            tenant_membership = exists().where(
                UserTenantRole.user_id == user_id,
                UserTenantRole.tenant_id == tenant_id,
            )
            result = await self._session.execute(select(User).where(User.id == user_id, tenant_membership))
            user = result.scalar_one_or_none()
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found in tenant '{tenant_id}'"))
            return Ok(self._user_to_dict(user))

    async def update_user(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        email: str | None = None,
        user_name: str | None = None,
        given_name: str | None = None,
        family_name: str | None = None,
    ) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.update_user",
            attributes={"tenant.id": str(tenant_id), "user.id": str(user_id)},
        ):
            tenant_membership = exists().where(
                UserTenantRole.user_id == user_id,
                UserTenantRole.tenant_id == tenant_id,
            )
            result = await self._session.execute(select(User).where(User.id == user_id, tenant_membership))
            user = result.scalar_one_or_none()
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found in tenant '{tenant_id}'"))

            if email is not None:
                user.email = email
            if user_name is not None:
                user.user_name = user_name
            if given_name is not None:
                user.given_name = given_name
            if family_name is not None:
                user.family_name = family_name
            user.updated_at = datetime.now(timezone.utc)

            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"User with email '{email}' already exists"))

            user_dict = self._user_to_dict(user)

            # Write-through sync (D7)
            sync_result = await self._adapter.sync_user(user_id=user.id, data=user_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after update_user: operation=%s user_id=%s payload=%s error=%s",
                    sync_err.operation,
                    user.id,
                    user_dict,
                    sync_err.message,
                )

            return Ok(user_dict)

    async def deactivate_user(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.deactivate_user",
            attributes={"tenant.id": str(tenant_id), "user.id": str(user_id)},
        ):
            tenant_membership = exists().where(
                UserTenantRole.user_id == user_id,
                UserTenantRole.tenant_id == tenant_id,
            )
            result = await self._session.execute(select(User).where(User.id == user_id, tenant_membership))
            user = result.scalar_one_or_none()
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found in tenant '{tenant_id}'"))

            user.status = UserStatus.inactive
            user.updated_at = datetime.now(timezone.utc)

            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Constraint violation during deactivation of user '{user_id}'"))

            user_dict = self._user_to_dict(user)

            # Write-through sync (D7)
            sync_result = await self._adapter.sync_user(user_id=user.id, data=user_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after deactivate_user: operation=%s user_id=%s payload=%s error=%s",
                    sync_err.operation,
                    user.id,
                    user_dict,
                    sync_err.message,
                )

            return Ok(user_dict)

    async def search_users(
        self, *, tenant_id: uuid.UUID, query: str = "", status: str | None = None
    ) -> Result[list[dict], IdentityError]:
        with tracer.start_as_current_span(
            "identity.search_users",
            attributes={"tenant.id": str(tenant_id)},
        ):
            # Scope by tenant via UserTenantRole join
            stmt = (
                select(User)
                .join(UserTenantRole, User.id == UserTenantRole.user_id)
                .where(UserTenantRole.tenant_id == tenant_id)
            )

            if status:
                stmt = stmt.where(User.status == status)

            if query:
                # Escape ILIKE metacharacters to prevent wildcard injection
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                like_pattern = f"%{escaped}%"
                stmt = stmt.where(
                    (User.email.ilike(like_pattern, escape="\\"))
                    | (User.user_name.ilike(like_pattern, escape="\\"))
                    | (User.given_name.ilike(like_pattern, escape="\\"))
                    | (User.family_name.ilike(like_pattern, escape="\\"))
                )

            stmt = stmt.distinct()
            result = await self._session.execute(stmt)
            users = result.scalars().all()
            return Ok([self._user_to_dict(u) for u in users])

    # --- Not implemented (stories 2.2+) ---

    async def create_role(
        self, *, tenant_id: uuid.UUID | None = None, name: str, description: str = ""
    ) -> Result[dict, IdentityError]:
        raise NotImplementedError("create_role is not yet implemented (story 2.2)")

    async def get_role(self, *, role_id: uuid.UUID) -> Result[dict, IdentityError]:
        raise NotImplementedError("get_role is not yet implemented (story 2.2)")

    async def update_role(
        self, *, role_id: uuid.UUID, name: str | None = None, description: str | None = None
    ) -> Result[dict, IdentityError]:
        raise NotImplementedError("update_role is not yet implemented (story 2.2)")

    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, IdentityError]:
        raise NotImplementedError("delete_role is not yet implemented (story 2.2)")

    async def create_permission(self, *, name: str, description: str = "") -> Result[dict, IdentityError]:
        raise NotImplementedError("create_permission is not yet implemented (story 2.2)")

    async def get_permission(self, *, permission_id: uuid.UUID) -> Result[dict, IdentityError]:
        raise NotImplementedError("get_permission is not yet implemented (story 2.2)")

    async def update_permission(
        self, *, permission_id: uuid.UUID, name: str | None = None, description: str | None = None
    ) -> Result[dict, IdentityError]:
        raise NotImplementedError("update_permission is not yet implemented (story 2.2)")

    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, IdentityError]:
        raise NotImplementedError("delete_permission is not yet implemented (story 2.2)")

    async def map_permission_to_role(
        self, *, role_id: uuid.UUID, permission_id: uuid.UUID
    ) -> Result[None, IdentityError]:
        raise NotImplementedError("map_permission_to_role is not yet implemented (story 2.2)")

    async def unmap_permission_from_role(
        self, *, role_id: uuid.UUID, permission_id: uuid.UUID
    ) -> Result[None, IdentityError]:
        raise NotImplementedError("unmap_permission_from_role is not yet implemented (story 2.2)")

    async def create_tenant(self, *, name: str, domains: list[str] | None = None) -> Result[dict, IdentityError]:
        raise NotImplementedError("create_tenant is not yet implemented (story 2.2)")

    async def get_tenant(self, *, tenant_id: uuid.UUID) -> Result[dict, IdentityError]:
        raise NotImplementedError("get_tenant is not yet implemented (story 2.2)")

    async def update_tenant(
        self, *, tenant_id: uuid.UUID, name: str | None = None, domains: list[str] | None = None
    ) -> Result[dict, IdentityError]:
        raise NotImplementedError("update_tenant is not yet implemented (story 2.2)")

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, IdentityError]:
        raise NotImplementedError("delete_tenant is not yet implemented (story 2.2)")

    async def assign_role_to_user(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
    ) -> Result[None, IdentityError]:
        raise NotImplementedError("assign_role_to_user is not yet implemented (story 2.2)")

    async def remove_role_from_user(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
    ) -> Result[None, IdentityError]:
        raise NotImplementedError("remove_role_from_user is not yet implemented (story 2.2)")

    async def get_tenant_users_with_roles(self, *, tenant_id: uuid.UUID) -> Result[list[dict], IdentityError]:
        raise NotImplementedError("get_tenant_users_with_roles is not yet implemented (story 2.2)")
