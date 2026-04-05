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

from app.errors.identity import Conflict, IdentityError, NotFound
from app.models.identity.user import User, UserStatus
from app.repositories.user import UserRepository
from app.services.adapters.base import IdentityProviderAdapter, SyncError

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
    ) -> None:
        self._repository = repository
        self._adapter = adapter

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
            user = await self._repository.create(user)

            self._log_sync_failure(
                await self._adapter.sync_user(
                    user_id=user.id,
                    data={"email": user.email, "status": user.status.value},
                ),
                user.id,
                "create",
            )

            await self._repository.commit()
            return Ok(user.model_dump())

    async def get_user(self, *, user_id: uuid.UUID) -> Result[dict, IdentityError]:
        """Retrieve a user by ID."""
        with tracer.start_as_current_span("UserService.get_user") as span:
            span.set_attribute("user.id", str(user_id))

            user = await self._repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))
            return Ok(user.model_dump())

    async def update_user(
        self,
        *,
        user_id: uuid.UUID,
        email: str | None = None,
        user_name: str | None = None,
        given_name: str | None = None,
        family_name: str | None = None,
    ) -> Result[dict, IdentityError]:
        """Update user fields, then sync to IdP."""
        with tracer.start_as_current_span("UserService.update_user") as span:
            span.set_attribute("user.id", str(user_id))

            user = await self._repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

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

            user = await self._repository.update(user)

            self._log_sync_failure(
                await self._adapter.sync_user(
                    user_id=user.id,
                    data={"email": user.email, "status": user.status.value},
                ),
                user.id,
                "update",
            )

            await self._repository.commit()
            return Ok(user.model_dump())

    async def deactivate_user(self, *, user_id: uuid.UUID) -> Result[dict, IdentityError]:
        """Set user status to inactive and sync to IdP.

        AC-2.1.4: repo sets status=inactive, adapter sync attempted.
        """
        with tracer.start_as_current_span("UserService.deactivate_user") as span:
            span.set_attribute("user.id", str(user_id))

            user = await self._repository.get(user_id)
            if user is None:
                return Error(NotFound(message=f"User '{user_id}' not found"))

            user.status = UserStatus.inactive
            user = await self._repository.update(user)

            self._log_sync_failure(
                await self._adapter.sync_user(
                    user_id=user.id,
                    data={"email": user.email, "status": user.status.value},
                ),
                user.id,
                "deactivate",
            )

            await self._repository.commit()
            return Ok(user.model_dump())

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
        match result:
            case Result(tag="error", error=sync_error):
                logger.warning(
                    "IdP sync failed for user %s (%s): %s",
                    entity_id,
                    operation,
                    sync_error.message,
                )
