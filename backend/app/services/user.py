"""UserService — domain orchestration for canonical User operations.

Middle layer of onion architecture: orchestrates UserRepository (data access)
and IdentityProviderAdapter (IdP sync). All methods return Result[T, IdentityError].
OTel spans on every method. Sync failure → log warning, still return Ok.
"""

from __future__ import annotations

import logging
import uuid

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, Forbidden, IdentityError, NotFound
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.base import RepositoryConflictError
from app.repositories.user import UserRepository
from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.cache_invalidation import CacheInvalidationPublisher

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class UserService:
    """Domain service for User operations.

    Orchestrates UserRepository (inner) and IdentityProviderAdapter (outer).
    Contains NO direct SQLAlchemy imports — uses repository methods only.
    """

    def __init__(
        self,
        *,
        repository: UserRepository,
        adapter: IdentityProviderAdapter,
        assignment_repository: UserTenantRoleRepository | None = None,
        publisher: CacheInvalidationPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._adapter = adapter
        self._assignment_repository = assignment_repository
        self._publisher = CacheInvalidationPublisher() if publisher is None else publisher

    async def create_user(
        self,
        *,
        tenant_id: uuid.UUID,
        email: str,
        user_name: str,
        given_name: str = "",
        family_name: str = "",
    ) -> Result[dict, IdentityError]:
        """Create a new user: persist via repo, then sync to IdP.

        AC-2.1.2: sync failure → log warning, still return Ok(user).
        """
        with tracer.start_as_current_span("UserService.create_user") as span:
            span.set_attribute("tenant.id", str(tenant_id))

            existing = await self._repository.get_by_email(email)
            if existing is not None:
                return Error(Conflict(message=f"User with email '{email}' already exists"))

            user = User(
                email=email,
                user_name=user_name,
                given_name=given_name,
                family_name=family_name,
            )
            try:
                user = await self._repository.create(user)
            except RepositoryConflictError:
                return Error(Conflict(message=f"User with email '{email}' already exists"))

            result_dict = user.model_dump()
            sync_data = {"email": user.email, "status": user.status.value}
            user_id = user.id
            try:
                await self._repository.commit()
            except Exception:
                logger.exception("Commit failed for create_user %s", user_id)
                return Error(Conflict(message="Failed to persist user"))

            await self._publisher.publish(
                entity_type="user", entity_id=user_id, operation="create", tenant_id=tenant_id
            )

            self._log_sync_failure(
                await self._adapter.sync_user(user_id=user_id, data=sync_data),
                user_id,
                "create",
            )

            return Ok(result_dict)

    async def get_user(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Retrieve a user by ID, scoped to the caller's tenant."""
        with tracer.start_as_current_span("UserService.get_user") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("user.id", str(user_id))

            user = await self._repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

            membership_err = await self._verify_tenant_membership(user_id, tenant_id)
            if membership_err is not None:
                return Error(membership_err)

            return Ok(user.model_dump())

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
        """Update user fields (tenant-scoped), then sync to IdP."""
        with tracer.start_as_current_span("UserService.update_user") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("user.id", str(user_id))

            user = await self._repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

            membership_err = await self._verify_tenant_membership(user_id, tenant_id)
            if membership_err is not None:
                return Error(membership_err)

            if email is not None:
                existing = await self._repository.get_by_email(email)
                if existing is not None and existing.id != user_id:
                    return Error(Conflict(message=f"User with email '{email}' already exists"))
                user.email = email
            if user_name is not None:
                user.user_name = user_name
            if given_name is not None:
                user.given_name = given_name
            if family_name is not None:
                user.family_name = family_name

            try:
                user = await self._repository.update(user)
            except RepositoryConflictError:
                return Error(Conflict(message="User update conflicts with existing data"))

            result_dict = user.model_dump()
            sync_data = {"email": user.email, "status": user.status.value}
            await self._repository.commit()

            await self._publisher.publish(
                entity_type="user", entity_id=user.id, operation="update", tenant_id=tenant_id
            )

            self._log_sync_failure(
                await self._adapter.sync_user(user_id=user.id, data=sync_data),
                user.id,
                "update",
            )

            return Ok(result_dict)

    async def _verify_tenant_membership(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> IdentityError | None:
        """Verify user has a role assignment in the given tenant. Returns error if not."""
        if not await self._repository.exists_in_tenant(user_id, tenant_id):
            return Forbidden(message="User does not belong to your tenant")
        return None

    async def deactivate_user(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Set user status to inactive and sync to IdP.

        AC-2.1.4: repo sets status=inactive, adapter sync attempted.
        """
        with tracer.start_as_current_span("UserService.deactivate_user") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("user.id", str(user_id))

            user = await self._repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

            membership_err = await self._verify_tenant_membership(user_id, tenant_id)
            if membership_err is not None:
                return Error(membership_err)

            user.status = UserStatus.inactive
            try:
                user = await self._repository.update(user)
            except RepositoryConflictError:
                return Error(Conflict(message="User deactivation conflicts with existing data"))

            result_dict = user.model_dump()
            sync_data = {"email": user.email, "status": user.status.value}
            await self._repository.commit()

            await self._publisher.publish(
                entity_type="user", entity_id=user.id, operation="deactivate", tenant_id=tenant_id
            )

            self._log_sync_failure(
                await self._adapter.sync_user(user_id=user.id, data=sync_data),
                user.id,
                "deactivate",
            )

            return Ok(result_dict)

    async def activate_user(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Set user status to active and sync to IdP.

        Mirrors deactivate_user for reactivation.
        """
        with tracer.start_as_current_span("UserService.activate_user") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("user.id", str(user_id))

            user = await self._repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

            membership_err = await self._verify_tenant_membership(user_id, tenant_id)
            if membership_err is not None:
                return Error(membership_err)

            user.status = UserStatus.active
            try:
                user = await self._repository.update(user)
            except RepositoryConflictError:
                return Error(Conflict(message="User activation conflicts with existing data"))

            result_dict = user.model_dump()
            sync_data = {"email": user.email, "status": user.status.value}
            await self._repository.commit()

            await self._publisher.publish(
                entity_type="user", entity_id=user.id, operation="activate", tenant_id=tenant_id
            )

            self._log_sync_failure(
                await self._adapter.sync_user(user_id=user.id, data=sync_data),
                user.id,
                "activate",
            )

            return Ok(result_dict)

    async def remove_user_from_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Remove a user's membership from a tenant by deleting all role assignments."""
        with tracer.start_as_current_span("UserService.remove_user_from_tenant") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("user.id", str(user_id))

            user = await self._repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

            membership_err = await self._verify_tenant_membership(user_id, tenant_id)
            if membership_err is not None:
                return Error(membership_err)

            if self._assignment_repository is None:
                return Error(NotFound(message="Assignment repository not configured"))

            deleted_count = await self._assignment_repository.delete_by_user_tenant(user_id, tenant_id)
            if deleted_count == 0:
                msg = f"No role assignments found for user '{user_id}' in tenant '{tenant_id}'"
                return Error(NotFound(message=msg))

            await self._assignment_repository.commit()

            await self._publisher.publish(
                entity_type="user", entity_id=user_id, operation="unassign", tenant_id=tenant_id
            )

            # Best-effort sync: notify IdP of membership removal
            self._log_sync_failure(
                await self._adapter.sync_user(
                    user_id=user_id,
                    data={"email": user.email, "status": user.status.value},
                ),
                user_id,
                "remove_user_from_tenant",
            )

            return Ok({"status": "removed", "user_id": str(user_id)})

    async def search_users(
        self,
        *,
        tenant_id: uuid.UUID,
        query: str = "",
    ) -> Result[list[dict], IdentityError]:
        """Search users scoped to a tenant.

        AC-2.1.3: delegates to repository for tenant-scoped filtering.
        """
        with tracer.start_as_current_span("UserService.search_users") as span:
            span.set_attribute("tenant.id", str(tenant_id))

            users = await self._repository.search(
                tenant_id=tenant_id,
                name=query if query else None,
            )
            return Ok([u.model_dump() for u in users])

    @staticmethod
    def _log_sync_failure(
        result: Result[None, SyncError],
        entity_id: uuid.UUID,
        operation: str,
    ) -> None:
        """Log adapter sync failures as warnings without propagating."""
        if result.is_error():
            logger.warning(
                "IdP sync failed for user %s (%s): %s",
                entity_id,
                operation,
                result.error.message,
            )
