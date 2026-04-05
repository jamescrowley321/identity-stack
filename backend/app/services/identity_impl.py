"""PostgresIdentityService — concrete IdentityService backed by Postgres + IdP adapter.

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
from app.models.identity.role import Permission, Role, RolePermission
from app.models.identity.tenant import Tenant, TenantStatus
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
                return Error(Conflict(message=f"User with email '{user.email}' already exists"))

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
                try:
                    status_enum = UserStatus(status)
                except ValueError:
                    return Ok([])  # Invalid status matches no users
                stmt = stmt.where(User.status == status_enum)

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

    # --- Role operations (Story 2.2) ---

    def _role_to_dict(self, role: Role) -> dict:
        return {
            "id": str(role.id),
            "name": role.name,
            "description": role.description,
            "tenant_id": str(role.tenant_id) if role.tenant_id else None,
            "created_at": role.created_at.isoformat() if role.created_at else None,
            "updated_at": role.updated_at.isoformat() if role.updated_at else None,
        }

    def _permission_to_dict(self, permission: Permission) -> dict:
        return {
            "id": str(permission.id),
            "name": permission.name,
            "description": permission.description,
            "created_at": permission.created_at.isoformat() if permission.created_at else None,
            "updated_at": permission.updated_at.isoformat() if permission.updated_at else None,
        }

    def _tenant_to_dict(self, tenant: Tenant) -> dict:
        return {
            "id": str(tenant.id),
            "name": tenant.name,
            "domains": tenant.domains if tenant.domains else [],
            "status": tenant.status.value if isinstance(tenant.status, TenantStatus) else tenant.status,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
            "updated_at": tenant.updated_at.isoformat() if tenant.updated_at else None,
        }

    async def create_role(
        self, *, tenant_id: uuid.UUID | None = None, name: str, description: str = ""
    ) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.create_role",
            attributes={"tenant.id": str(tenant_id) if tenant_id else "global"},
        ):
            role = Role(name=name, description=description, tenant_id=tenant_id)
            self._session.add(role)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                scope = f"tenant '{tenant_id}'" if tenant_id else "global scope"
                return Error(Conflict(message=f"Role '{name}' already exists in {scope}"))

            role_dict = self._role_to_dict(role)

            sync_result = await self._adapter.sync_role(role_id=role.id, data=role_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after create_role: operation=%s role_id=%s error=%s",
                    sync_err.operation,
                    role.id,
                    sync_err.message,
                )

            return Ok(role_dict)

    async def get_role(self, *, role_id: uuid.UUID) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.get_role",
            attributes={"role.id": str(role_id)},
        ):
            result = await self._session.execute(select(Role).where(Role.id == role_id))
            role = result.scalar_one_or_none()
            if role is None:
                return Error(NotFound(message=f"Role '{role_id}' not found"))
            return Ok(self._role_to_dict(role))

    async def update_role(
        self, *, role_id: uuid.UUID, name: str | None = None, description: str | None = None
    ) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.update_role",
            attributes={"role.id": str(role_id)},
        ):
            result = await self._session.execute(select(Role).where(Role.id == role_id))
            role = result.scalar_one_or_none()
            if role is None:
                return Error(NotFound(message=f"Role '{role_id}' not found"))

            old_name = role.name
            if name is not None:
                role.name = name
            if description is not None:
                role.description = description
            role.updated_at = datetime.now(timezone.utc)

            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                scope = f"tenant '{role.tenant_id}'" if role.tenant_id else "global scope"
                return Error(Conflict(message=f"Role '{role.name}' already exists in {scope}"))

            role_dict = self._role_to_dict(role)
            role_dict["old_name"] = old_name

            sync_result = await self._adapter.sync_role(role_id=role.id, data=role_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after update_role: operation=%s role_id=%s error=%s",
                    sync_err.operation,
                    role.id,
                    sync_err.message,
                )

            return Ok(role_dict)

    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, IdentityError]:
        with tracer.start_as_current_span(
            "identity.delete_role",
            attributes={"role.id": str(role_id)},
        ):
            result = await self._session.execute(select(Role).where(Role.id == role_id))
            role = result.scalar_one_or_none()
            if role is None:
                return Error(NotFound(message=f"Role '{role_id}' not found"))

            role_name = role.name
            await self._session.delete(role)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Role '{role_id}' cannot be deleted — it has dependent assignments"))

            sync_result = await self._adapter.delete_role(role_id=role_id, role_name=role_name)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after delete_role: operation=%s role_id=%s error=%s",
                    sync_err.operation,
                    role_id,
                    sync_err.message,
                )

            return Ok(None)

    # --- Permission operations (Story 2.2) ---

    async def create_permission(self, *, name: str, description: str = "") -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.create_permission",
            attributes={"permission.name": name},
        ):
            permission = Permission(name=name, description=description)
            self._session.add(permission)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Permission '{name}' already exists"))

            perm_dict = self._permission_to_dict(permission)

            sync_result = await self._adapter.sync_permission(permission_id=permission.id, data=perm_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after create_permission: operation=%s permission_id=%s error=%s",
                    sync_err.operation,
                    permission.id,
                    sync_err.message,
                )

            return Ok(perm_dict)

    async def get_permission(self, *, permission_id: uuid.UUID) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.get_permission",
            attributes={"permission.id": str(permission_id)},
        ):
            result = await self._session.execute(select(Permission).where(Permission.id == permission_id))
            permission = result.scalar_one_or_none()
            if permission is None:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))
            return Ok(self._permission_to_dict(permission))

    async def update_permission(
        self, *, permission_id: uuid.UUID, name: str | None = None, description: str | None = None
    ) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.update_permission",
            attributes={"permission.id": str(permission_id)},
        ):
            result = await self._session.execute(select(Permission).where(Permission.id == permission_id))
            permission = result.scalar_one_or_none()
            if permission is None:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))

            old_name = permission.name
            if name is not None:
                permission.name = name
            if description is not None:
                permission.description = description
            permission.updated_at = datetime.now(timezone.utc)

            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Permission '{permission.name}' already exists"))

            perm_dict = self._permission_to_dict(permission)
            perm_dict["old_name"] = old_name

            sync_result = await self._adapter.sync_permission(permission_id=permission.id, data=perm_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after update_permission: operation=%s permission_id=%s error=%s",
                    sync_err.operation,
                    permission.id,
                    sync_err.message,
                )

            return Ok(perm_dict)

    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, IdentityError]:
        with tracer.start_as_current_span(
            "identity.delete_permission",
            attributes={"permission.id": str(permission_id)},
        ):
            result = await self._session.execute(select(Permission).where(Permission.id == permission_id))
            permission = result.scalar_one_or_none()
            if permission is None:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))

            permission_name = permission.name
            await self._session.delete(permission)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(
                    Conflict(message=f"Permission '{permission_id}' cannot be deleted — it has dependent mappings")
                )

            sync_result = await self._adapter.delete_permission(
                permission_id=permission_id, permission_name=permission_name
            )
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after delete_permission: operation=%s permission_id=%s error=%s",
                    sync_err.operation,
                    permission_id,
                    sync_err.message,
                )

            return Ok(None)

    async def map_permission_to_role(
        self, *, role_id: uuid.UUID, permission_id: uuid.UUID
    ) -> Result[None, IdentityError]:
        with tracer.start_as_current_span(
            "identity.map_permission_to_role",
            attributes={"role.id": str(role_id), "permission.id": str(permission_id)},
        ):
            # Validate role exists
            role_result = await self._session.execute(select(Role).where(Role.id == role_id))
            if role_result.scalar_one_or_none() is None:
                return Error(NotFound(message=f"Role '{role_id}' not found"))

            # Validate permission exists
            perm_result = await self._session.execute(select(Permission).where(Permission.id == permission_id))
            if perm_result.scalar_one_or_none() is None:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))

            mapping = RolePermission(role_id=role_id, permission_id=permission_id)
            self._session.add(mapping)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Permission '{permission_id}' is already mapped to role '{role_id}'"))

            return Ok(None)

    async def unmap_permission_from_role(
        self, *, role_id: uuid.UUID, permission_id: uuid.UUID
    ) -> Result[None, IdentityError]:
        with tracer.start_as_current_span(
            "identity.unmap_permission_from_role",
            attributes={"role.id": str(role_id), "permission.id": str(permission_id)},
        ):
            result = await self._session.execute(
                select(RolePermission).where(
                    RolePermission.role_id == role_id,
                    RolePermission.permission_id == permission_id,
                )
            )
            mapping = result.scalar_one_or_none()
            if mapping is None:
                return Error(NotFound(message=f"Permission '{permission_id}' is not mapped to role '{role_id}'"))

            await self._session.delete(mapping)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                msg = f"Cannot unmap permission '{permission_id}' from role '{role_id}'"
                return Error(Conflict(message=msg))
            return Ok(None)

    # --- Tenant operations (Story 2.2) ---

    async def create_tenant(self, *, name: str, domains: list[str] | None = None) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.create_tenant",
            attributes={"tenant.name": name},
        ):
            tenant = Tenant(name=name, domains=domains or [])
            self._session.add(tenant)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Tenant '{name}' already exists"))

            tenant_dict = self._tenant_to_dict(tenant)

            sync_result = await self._adapter.sync_tenant(tenant_id=tenant.id, data=tenant_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after create_tenant: operation=%s tenant_id=%s error=%s",
                    sync_err.operation,
                    tenant.id,
                    sync_err.message,
                )

            return Ok(tenant_dict)

    async def get_tenant(self, *, tenant_id: uuid.UUID) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.get_tenant",
            attributes={"tenant.id": str(tenant_id)},
        ):
            result = await self._session.execute(select(Tenant).where(Tenant.id == tenant_id))
            tenant = result.scalar_one_or_none()
            if tenant is None:
                return Error(NotFound(message=f"Tenant '{tenant_id}' not found"))
            return Ok(self._tenant_to_dict(tenant))

    async def update_tenant(
        self, *, tenant_id: uuid.UUID, name: str | None = None, domains: list[str] | None = None
    ) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.update_tenant",
            attributes={"tenant.id": str(tenant_id)},
        ):
            result = await self._session.execute(select(Tenant).where(Tenant.id == tenant_id))
            tenant = result.scalar_one_or_none()
            if tenant is None:
                return Error(NotFound(message=f"Tenant '{tenant_id}' not found"))

            if name is not None:
                tenant.name = name
            if domains is not None:
                tenant.domains = domains
            tenant.updated_at = datetime.now(timezone.utc)

            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Tenant '{tenant.name}' already exists"))

            tenant_dict = self._tenant_to_dict(tenant)

            sync_result = await self._adapter.sync_tenant(tenant_id=tenant.id, data=tenant_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after update_tenant: operation=%s tenant_id=%s error=%s",
                    sync_err.operation,
                    tenant.id,
                    sync_err.message,
                )

            return Ok(tenant_dict)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, IdentityError]:
        with tracer.start_as_current_span(
            "identity.delete_tenant",
            attributes={"tenant.id": str(tenant_id)},
        ):
            result = await self._session.execute(select(Tenant).where(Tenant.id == tenant_id))
            tenant = result.scalar_one_or_none()
            if tenant is None:
                return Error(NotFound(message=f"Tenant '{tenant_id}' not found"))

            await self._session.delete(tenant)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(
                    Conflict(message=f"Tenant '{tenant_id}' cannot be deleted — it has dependent assignments or roles")
                )

            sync_result = await self._adapter.delete_tenant(tenant_id=tenant_id)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after delete_tenant: operation=%s tenant_id=%s error=%s",
                    sync_err.operation,
                    tenant_id,
                    sync_err.message,
                )

            return Ok(None)

    # --- Role assignment operations (Story 2.2) ---

    async def assign_role_to_user(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
    ) -> Result[None, IdentityError]:
        with tracer.start_as_current_span(
            "identity.assign_role_to_user",
            attributes={
                "tenant.id": str(tenant_id),
                "user.id": str(user_id),
                "role.id": str(role_id),
            },
        ):
            # Validate referenced entities exist before insert
            user_result = await self._session.execute(select(User).where(User.id == user_id))
            if user_result.scalar_one_or_none() is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

            tenant_result = await self._session.execute(select(Tenant).where(Tenant.id == tenant_id))
            if tenant_result.scalar_one_or_none() is None:
                return Error(NotFound(message=f"Tenant '{tenant_id}' not found"))

            role_result = await self._session.execute(select(Role).where(Role.id == role_id))
            role = role_result.scalar_one_or_none()
            if role is None:
                return Error(NotFound(message=f"Role '{role_id}' not found"))

            assignment = UserTenantRole(
                user_id=user_id,
                tenant_id=tenant_id,
                role_id=role_id,
            )
            self._session.add(assignment)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(
                    Conflict(
                        message=f"Role '{role_id}' is already assigned to user '{user_id}' in tenant '{tenant_id}'"
                    )
                )

            sync_result = await self._adapter.sync_role_assignment(
                user_id=user_id, tenant_id=tenant_id, role_id=role_id, role_name=role.name
            )
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after assign_role_to_user: operation=%s user_id=%s tenant_id=%s role_id=%s error=%s",
                    sync_err.operation,
                    user_id,
                    tenant_id,
                    role_id,
                    sync_err.message,
                )

            return Ok(None)

    async def remove_role_from_user(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, role_id: uuid.UUID
    ) -> Result[None, IdentityError]:
        with tracer.start_as_current_span(
            "identity.remove_role_from_user",
            attributes={
                "tenant.id": str(tenant_id),
                "user.id": str(user_id),
                "role.id": str(role_id),
            },
        ):
            result = await self._session.execute(
                select(UserTenantRole).where(
                    UserTenantRole.user_id == user_id,
                    UserTenantRole.tenant_id == tenant_id,
                    UserTenantRole.role_id == role_id,
                )
            )
            assignment = result.scalar_one_or_none()
            if assignment is None:
                return Error(
                    NotFound(message=f"Role '{role_id}' is not assigned to user '{user_id}' in tenant '{tenant_id}'")
                )

            # Fetch role name before deletion to avoid race with concurrent role delete
            role_result = await self._session.execute(select(Role).where(Role.id == role_id))
            role = role_result.scalar_one_or_none()

            await self._session.delete(assignment)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(
                    Conflict(message=f"Cannot remove role '{role_id}' from user '{user_id}' — constraint violation")
                )

            # Write-through: sync role removal to IdP (D7)
            if role:
                sync_result = await self._adapter.remove_role_assignment(
                    user_id=user_id, tenant_id=tenant_id, role_id=role_id, role_name=role.name
                )
                if sync_result.is_error():
                    sync_err = sync_result.error
                    logger.warning(
                        "Sync failed after remove_role_from_user: operation=%s user_id=%s role_id=%s error=%s",
                        sync_err.operation,
                        user_id,
                        role_id,
                        sync_err.message,
                    )

            return Ok(None)

    async def get_tenant_users_with_roles(self, *, tenant_id: uuid.UUID) -> Result[list[dict], IdentityError]:
        with tracer.start_as_current_span(
            "identity.get_tenant_users_with_roles",
            attributes={"tenant.id": str(tenant_id)},
        ):
            # Verify tenant exists
            tenant_result = await self._session.execute(select(Tenant).where(Tenant.id == tenant_id))
            if tenant_result.scalar_one_or_none() is None:
                return Error(NotFound(message=f"Tenant '{tenant_id}' not found"))

            # Fetch all assignments with user and role data
            stmt = (
                select(User, Role, UserTenantRole)
                .join(UserTenantRole, User.id == UserTenantRole.user_id)
                .join(Role, Role.id == UserTenantRole.role_id)
                .where(UserTenantRole.tenant_id == tenant_id)
            )
            result = await self._session.execute(stmt)
            rows = result.all()

            # Group by user
            users_map: dict[uuid.UUID, dict] = {}
            for user, role, _assignment in rows:
                if user.id not in users_map:
                    user_dict = self._user_to_dict(user)
                    user_dict["roles"] = []
                    users_map[user.id] = user_dict
                users_map[user.id]["roles"].append(self._role_to_dict(role))

            return Ok(list(users_map.values()))

    # --- Lookup operations (Story 2.3) ---

    async def list_roles(self, *, tenant_id: uuid.UUID | None = None) -> Result[list[dict], IdentityError]:
        with tracer.start_as_current_span(
            "identity.list_roles",
            attributes={"tenant.id": str(tenant_id) if tenant_id else "all"},
        ):
            stmt = select(Role)
            if tenant_id is not None:
                stmt = stmt.where(Role.tenant_id == tenant_id)
            result = await self._session.execute(stmt)
            roles = result.scalars().all()
            return Ok([self._role_to_dict(r) for r in roles])

    async def list_permissions(self) -> Result[list[dict], IdentityError]:
        with tracer.start_as_current_span("identity.list_permissions"):
            result = await self._session.execute(select(Permission))
            permissions = result.scalars().all()
            return Ok([self._permission_to_dict(p) for p in permissions])

    async def get_role_by_name(self, *, name: str, tenant_id: uuid.UUID | None = None) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.get_role_by_name",
            attributes={"role.name": name},
        ):
            stmt = select(Role).where(Role.name == name)
            if tenant_id is not None:
                stmt = stmt.where(Role.tenant_id == tenant_id)
            result = await self._session.execute(stmt)
            role = result.scalars().first()
            if role is None:
                return Error(NotFound(message=f"Role '{name}' not found"))
            return Ok(self._role_to_dict(role))

    async def get_permission_by_name(self, *, name: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.get_permission_by_name",
            attributes={"permission.name": name},
        ):
            result = await self._session.execute(select(Permission).where(Permission.name == name))
            permission = result.scalar_one_or_none()
            if permission is None:
                return Error(NotFound(message=f"Permission '{name}' not found"))
            return Ok(self._permission_to_dict(permission))

    async def activate_user(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span(
            "identity.activate_user",
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

            user.status = UserStatus.active
            user.updated_at = datetime.now(timezone.utc)

            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Constraint violation during activation of user '{user_id}'"))

            user_dict = self._user_to_dict(user)

            # Write-through sync (D7)
            sync_result = await self._adapter.sync_user(user_id=user.id, data=user_dict)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after activate_user: operation=%s user_id=%s error=%s",
                    sync_err.operation,
                    user.id,
                    sync_err.message,
                )

            return Ok(user_dict)

    async def remove_user_from_tenant(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Result[None, IdentityError]:
        with tracer.start_as_current_span(
            "identity.remove_user_from_tenant",
            attributes={"tenant.id": str(tenant_id), "user.id": str(user_id)},
        ):
            result = await self._session.execute(
                select(UserTenantRole).where(
                    UserTenantRole.user_id == user_id,
                    UserTenantRole.tenant_id == tenant_id,
                )
            )
            assignments = result.scalars().all()
            if not assignments:
                return Error(NotFound(message=f"User '{user_id}' has no roles in tenant '{tenant_id}'"))

            for assignment in assignments:
                await self._session.delete(assignment)
            try:
                await self._session.flush()
            except IntegrityError:
                await self._session.rollback()
                return Error(Conflict(message=f"Cannot remove user '{user_id}' from tenant '{tenant_id}'"))

            # Write-through sync (D7)
            sync_result = await self._adapter.remove_user_from_tenant(user_id=user_id, tenant_id=tenant_id)
            if sync_result.is_error():
                sync_err = sync_result.error
                logger.warning(
                    "Sync failed after remove_user_from_tenant: operation=%s user_id=%s tenant_id=%s error=%s",
                    sync_err.operation,
                    user_id,
                    tenant_id,
                    sync_err.message,
                )

            return Ok(None)
