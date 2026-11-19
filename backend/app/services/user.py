"""UserService — domain orchestration for canonical User operations.

Middle layer of onion architecture: orchestrates UserRepository (data access)
and IdentityProviderAdapter (IdP sync). All methods return Result[T, IdentityError].
OTel spans on every method. Sync failure → log warning, still return Ok.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, Forbidden, IdentityError, NotFound, ValidationError
from app.logbook import logbook
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.base import RepositoryConflictError
from app.repositories.user import UserRepository
from app.services.adapters.base import (
    CHANGED_IN_TENANT,
    CREATED_IN_TENANT,
    PROVIDER_SYNC,
    PROVIDER_SYNC_FAILED,
    STATUS_CHANGED,
    IdentityProviderAdapter,
    SyncError,
)
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.memberships import restore_memberships

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

T = TypeVar("T")


class MemberStatus(enum.StrEnum):
    """A tenant member's status as the members API names it (the SPA's vocabulary, Descope's)."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    INVITED = "invited"


# The canonical user's status, as a member's: a provisioned user has not signed in yet.
_MEMBER_STATUS: dict[UserStatus, MemberStatus] = {
    UserStatus.active: MemberStatus.ENABLED,
    UserStatus.inactive: MemberStatus.DISABLED,
    UserStatus.provisioned: MemberStatus.INVITED,
}

# The status a member may be set to, as the canonical user's.
_SETTABLE_STATUS: dict[MemberStatus, UserStatus] = {
    MemberStatus.ENABLED: UserStatus.active,
    MemberStatus.DISABLED: UserStatus.inactive,
}


class MemberRefusal(enum.StrEnum):
    """Why a member operation was refused: an ``IdentityError``'s message."""

    # A user id that names no member of the tenant, whether or not the user exists.
    NOT_IN_TENANT = "Member not found in tenant"
    # The canonical status is the account's, not one membership's (as Descope's is, #581).
    IN_ANOTHER_TENANT = "Member also belongs to another tenant"
    # An invitation creates the account: it never enrols one that exists, which
    # another tenant may hold, nor tells whose it is (as Descope's invite refuses).
    ACCOUNT_EXISTS = "An account with this email already exists"


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
            sync_data = {
                "email": user.email,
                "status": user.status.value,
                "user_name": user.user_name,
                "given_name": user.given_name,
                "family_name": user.family_name,
                CREATED_IN_TENANT: str(tenant_id),
            }
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
            sync_data = {
                "email": user.email,
                "status": user.status.value,
                STATUS_CHANGED: True,
                CHANGED_IN_TENANT: str(tenant_id),
            }
            await self._repository.commit()

            await self._publisher.publish(
                entity_type="user", entity_id=user.id, operation="deactivate", tenant_id=tenant_id
            )

            if self._revocation_failed(
                await self._adapter.sync_user(user_id=user.id, data=sync_data), user.id, "deactivate"
            ):
                result_dict[PROVIDER_SYNC] = PROVIDER_SYNC_FAILED

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
            sync_data = {
                "email": user.email,
                "status": user.status.value,
                STATUS_CHANGED: True,
                CHANGED_IN_TENANT: str(tenant_id),
            }
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
            payload: dict = {"status": "removed", "user_id": str(user_id)}
            pushed = await self._adapter.sync_user(
                user_id=user_id,
                data={"email": user.email, "status": user.status.value, "left_tenant_id": str(tenant_id)},
            )
            if self._revocation_failed(pushed, user_id, "remove_user_from_tenant"):
                payload[PROVIDER_SYNC] = PROVIDER_SYNC_FAILED
            elif self._adapter.ends_memberships is True and pushed.is_ok():
                # A role given in the tenant while the user left it pushed its
                # membership before the removal, which undid it: it is added back.
                await restore_memberships(self._assignment_repository, self._adapter, [(user_id, tenant_id)])

            return Ok(payload)

    async def list_members(self, *, tenant_id: uuid.UUID) -> Result[list[dict], IdentityError]:
        """The tenant's members, each with its roles there, in the members API's shape."""
        with tracer.start_as_current_span("UserService.list_members") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            members: dict[uuid.UUID, dict] = {}
            for user, role_name in await self._repository.list_tenant_members(tenant_id):
                member = members.setdefault(user.id, _member(user))
                member["roleNames"].append(role_name)
            span.set_attribute("members.count", len(members))
            return Ok(list(members.values()))

    async def create_member(
        self,
        *,
        tenant_id: uuid.UUID,
        email: str,
        stage_memberships: Callable[[uuid.UUID], Awaitable[Result[T, IdentityError]]],
    ) -> Result[tuple[dict, T], IdentityError]:
        """Create the user with ``email`` and its memberships in one transaction, then push the user.

        ``stage_memberships`` writes the new user's role assignments in the same
        transaction without committing (``RoleService.stage_member_roles``); its
        ``Ok`` value is returned beside the member for the caller to push after
        the commit. An existing account with that email is ``Conflict``
        (``MemberRefusal.ACCOUNT_EXISTS``), whichever tenant holds it. On any
        ``Error`` nothing is committed.
        """
        with tracer.start_as_current_span("UserService.create_member") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            if await self._repository.get_by_email(email) is not None:
                return Error(Conflict(message=MemberRefusal.ACCOUNT_EXISTS))
            try:
                user = await self._repository.create(User(email=email, user_name=email))
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(Conflict(message=MemberRefusal.ACCOUNT_EXISTS))
            staged = await stage_memberships(user.id)
            if staged.is_error():
                await self._repository.rollback()
                return Error(staged.error)
            member = {"userId": str(user.id), "email": user.email}
            sync_data = {"email": user.email, "status": user.status.value, CREATED_IN_TENANT: str(tenant_id)}
            user_id = user.id
            try:
                await self._repository.commit()
            except Exception:
                logger.exception("Commit failed for create_member %s", user_id)
                await self._repository.rollback()
                return Error(Conflict(message="Failed to persist user"))

            await self._publisher.publish(
                entity_type="user", entity_id=user_id, operation="create", tenant_id=tenant_id
            )
            self._log_sync_failure(await self._adapter.sync_user(user_id=user_id, data=sync_data), user_id, "create")
            return Ok((member, staged.ok))

    async def set_member_status(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, status: MemberStatus
    ) -> Result[dict, IdentityError]:
        """Enable or disable a member whose only tenant is ``tenant_id``, and push the change.

        The status is the canonical account's, which every tenant of the user
        reads, so a member who also belongs to another tenant is ``Conflict``:
        this tenant alone cannot decide it. A user who is no member of the tenant
        is ``Forbidden``, the same answer whether or not the user exists.
        """
        with tracer.start_as_current_span("UserService.set_member_status") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("user.id", str(user_id))
            wanted = _SETTABLE_STATUS.get(status)
            if wanted is None:
                return Error(ValidationError(message=f"A member's status is set to one of {list(_SETTABLE_STATUS)}"))
            if self._assignment_repository is None:
                return Error(NotFound(message="Assignment repository not configured"))
            # The user's row is held until the status commits: an assignment of the
            # user in another tenant waits on it (its foreign key), so the user
            # cannot join one between the check and the write.
            await self._repository.lock(user_id)
            tenants = {row.tenant_id for row in await self._assignment_repository.list_by_user(user_id)}
            refusal = None
            if tenant_id not in tenants:
                refusal = Forbidden(message=MemberRefusal.NOT_IN_TENANT)
            elif tenants != {tenant_id}:
                refusal = Conflict(message=MemberRefusal.IN_ANOTHER_TENANT)
            if refusal is not None:
                await self._repository.rollback()
                return Error(refusal)
            if wanted is UserStatus.inactive:
                return await self.deactivate_user(tenant_id=tenant_id, user_id=user_id)
            return await self.activate_user(tenant_id=tenant_id, user_id=user_id)

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

    async def list_canonical_users(
        self,
        *,
        status: UserStatus | None = None,
        limit: int = 50,
    ) -> list[User]:
        """List canonical users (cross-tenant) with optional status filter."""
        with tracer.start_as_current_span("UserService.list_canonical_users") as span:
            span.set_attribute("users.limit", limit)
            if status is not None:
                span.set_attribute("users.status_filter", status.value)
            return await self._repository.list_filtered(status=status, limit=limit)

    def _revocation_failed(self, result: Result[None, SyncError], user_id: uuid.UUID, operation: str) -> bool:
        """Whether a push that takes access away failed where the adapter keeps memberships (Keycloak).

        There it is logged as an error and told to the caller: the provider still
        grants what the canonical change took away. Elsewhere it is logged as
        every sync failure is, and nothing is told.
        """
        if self._adapter.ends_memberships is not True:
            self._log_sync_failure(result, user_id, operation)
            return False
        if result.is_ok():
            return False
        logger.error("IdP revocation failed for user %s (%s): %s", user_id, operation, result.error.message)
        return True

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


def _member(user: User) -> dict:
    """A canonical user as a tenant member, before its roles are added."""
    name = " ".join(part for part in (user.given_name, user.family_name) if part)
    return {
        "userId": str(user.id),
        "email": user.email,
        "name": name or user.user_name,
        "status": _MEMBER_STATUS[user.status],
        "roleNames": [],
    }
