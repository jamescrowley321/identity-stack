"""IdentityProviderAdapter ABC — outbound sync interface to external IdPs.

All methods return Result[None, SyncError] — never raise for domain errors.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from expression import Result

# ``delete_role_assignment`` data key: ``True`` when the removed assignment was the
# user's last role in the tenant, so they no longer belong to it (RoleService).
LAST_ROLE_IN_TENANT = "last_role_in_tenant"

# ``sync_user`` data key: ``True`` when the change is the user's status (activate,
# deactivate). Without it an adapter does not push the status, so a profile update
# does not undo an enable or disable made in the identity provider (UserService).
STATUS_CHANGED = "status_changed"

# ``SyncError.context`` key: ``True`` when the provider gave no readable answer (no
# response, a timeout). A service pushing many changes stops at the first such
# failure instead of waiting out the timeout once per change (RoleService.delete_role).
UNANSWERED = "unanswered"

# Response key a service adds, set to PROVIDER_SYNC_FAILED, when an adapter that
# keeps memberships (``ends_memberships``) could not end one or disable an account:
# the canonical change is committed, but the identity provider still grants it.
PROVIDER_SYNC = "provider_sync"
PROVIDER_SYNC_FAILED = "failed"

# ``sync_user`` data key: the id of the tenant a user was just created in
# (UserService.create_user), so an adapter that keeps users in the tenant's
# organization (Zitadel) creates the user there. Absent on every other change.
CREATED_IN_TENANT = "created_in_tenant"

# ``sync_user`` data key, sent with ``STATUS_CHANGED``: the id of the tenant the status
# was changed in (UserService.activate_user, deactivate_user). An adapter that keeps
# each user in one tenant's organization (Zitadel) pushes the change only to a user of
# that tenant's organization: an admin of another tenant the user was given a role in
# does not reach the user's account.
CHANGED_IN_TENANT = "changed_in_tenant"


@dataclass(frozen=True)
class SyncError:
    """Error returned when an adapter sync operation fails."""

    message: str
    operation: str = ""
    context: dict | None = field(default=None)


class IdentityProviderAdapter(ABC):
    """Abstract base class for outbound identity provider sync operations.

    Implementations push canonical state changes to external IdPs (e.g. Descope).
    """

    # True for an adapter that keeps a tenant membership beside the role
    # assignments (Keycloak organizations) and ends it on ``delete_role_assignment``
    # with ``LAST_ROLE_IN_TENANT``. ``RoleService.delete_role`` then tells it which
    # users the deleted role was the last role of; other adapters get the role
    # delete alone.
    ends_memberships: ClassVar[bool] = False

    @abstractmethod
    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]: ...

    @abstractmethod
    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]: ...

    @abstractmethod
    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]: ...

    @abstractmethod
    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]: ...

    @abstractmethod
    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]: ...
