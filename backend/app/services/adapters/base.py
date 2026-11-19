"""IdentityProviderAdapter ABC — outbound sync interface to external IdPs.

All methods return Result[None, SyncError] — never raise for domain errors.
"""

from __future__ import annotations

import enum
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from expression import Result

from app.services.relationship_store import RelationshipStoreKind

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


# ``sync_user`` data key: the caller's own subject at the adapter's provider, sent with
# ``given_name`` and ``family_name`` when the account's owner changed their own names
# (UserService.update_own_names). The adapter pushes the names to that account, and
# to no other: no other field and no status. Sent only for a principal of the sync
# provider, so the subject is one of its accounts.
OWN_PROFILE_OF = "own_profile_of"


class Capability(enum.StrEnum):
    """An optional feature a sync adapter implements beyond the canonical sync.

    ``GET /api/capabilities`` reports the configured adapter's set, the SPA shows a
    page only when its capability is there, and a route for a capability the
    adapter lacks answers 404 (``app.dependencies.capabilities``).
    """

    # Credentials a machine signs in with, owned by a tenant: Descope access keys,
    # and for a canonical provider a client the adapter creates through
    # ``MachineCredentialPush`` (Keycloak: a confidential client with a service account).
    MACHINE_CREDENTIALS = "machine_credentials"
    # Free-form attributes on a user and on a tenant: the profile's custom
    # attributes and the tenant settings (``CustomAttributes``).
    CUSTOM_ATTRIBUTES = "custom_attributes"


# One custom attribute's value, as the API takes and answers it.
AttributeValue = str | int | bool | float | None


@dataclass(frozen=True)
class SyncError:
    """Error returned when an adapter sync operation fails."""

    message: str
    operation: str = ""
    context: dict | None = field(default=None)


@dataclass(frozen=True)
class MachineClient:
    """A client the provider created for a machine credential (``MachineCredentialPush.create_machine_client``).

    ``client_id`` is what the machine authenticates as and ``secret`` its secret,
    returned once and never stored. ``subject`` is the ``sub`` of the tokens the
    client is issued and ``external_id`` the provider's id for the client.
    ``registration_token`` is the provider's token for managing that one client
    later: it is stored, but never returned by the API.
    """

    client_id: str
    external_id: str
    subject: str
    secret: str = field(repr=False)
    registration_token: str = field(repr=False, default="")


@dataclass(frozen=True)
class MachineClientRef:
    """What the adapter needs to change or delete a machine credential's client."""

    client_id: str
    external_id: str
    subject: str
    registration_token: str = field(repr=False, default="")


class MachineCredentialPush(ABC):
    """The provider half of the ``machine_credentials`` capability of a canonical adapter.

    The canonical service (``app.services.machine_credentials``) keeps each
    credential in Postgres and pushes it here. Every failure is ``Error(SyncError)``.
    """

    @abstractmethod
    async def create_machine_client(
        self, *, credential_id: uuid.UUID, tenant_id: uuid.UUID, name: str
    ) -> Result[MachineClient, SyncError]:
        """Create a client that signs in with the client-credentials grant only, owned by the tenant."""

    @abstractmethod
    async def set_machine_client_enabled(self, *, client: MachineClientRef, enabled: bool) -> Result[None, SyncError]:
        """Let the client be issued tokens, or stop it."""

    @abstractmethod
    async def delete_machine_client(self, *, client: MachineClientRef) -> Result[None, SyncError]:
        """Delete the client. One that is already gone is deleted."""


@dataclass(frozen=True)
class TenantAttributes:
    """A tenant's name and custom attributes, as its provider holds them."""

    name: str
    attributes: dict[str, AttributeValue]


class CustomAttributes(ABC):
    """The ``custom_attributes`` capability: free-form attributes on the caller and on the request's tenant.

    An adapter that declares ``Capability.CUSTOM_ATTRIBUTES`` implements this
    (``IdentityProviderAdapter.__init_subclass__``). The capability serves only the
    sync provider's own principals (``app.dependencies.capabilities.served``), so
    ``subject`` is the caller's own ``sub`` at this provider, and ``tenant`` the
    request's tenant as the tenant resolver gives it to them (conventions §18):
    Descope's own tenant id for Descope, the canonical tenant id for a provider
    authorized from the canonical model, which the adapter maps to its organization.
    A value of ``None`` removes the attribute.
    """

    @abstractmethod
    async def user_attributes(self, *, subject: str) -> Result[dict[str, AttributeValue], SyncError]: ...

    @abstractmethod
    async def set_user_attribute(self, *, subject: str, key: str, value: AttributeValue) -> Result[None, SyncError]: ...

    @abstractmethod
    async def tenant_attributes(self, *, tenant: str) -> Result[TenantAttributes | None, SyncError]:
        """``Ok(None)`` when the provider has no such tenant."""

    @abstractmethod
    async def set_tenant_attributes(
        self, *, tenant: str, attributes: dict[str, AttributeValue]
    ) -> Result[None, SyncError]: ...


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

    # The optional capabilities the adapter implements. Every concrete adapter
    # declares its own set, empty when it implements none.
    capabilities: ClassVar[frozenset[Capability]]

    # The relationship store a deployment syncing to the provider gets when
    # RELATIONSHIP_STORE is unset: OpenFGA, for a provider with no Zanzibar-style
    # store of its own (Keycloak, Zitadel, Ory: owner decision 1 of #672). Descope
    # declares its own FGA store.
    relationship_store: ClassVar[RelationshipStoreKind] = RelationshipStoreKind.OPENFGA

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse an adapter class that does not declare its ``capabilities``, or lacks one's interface."""
        super().__init_subclass__(**kwargs)
        capabilities = getattr(cls, "capabilities", None)
        if not isinstance(capabilities, frozenset):
            raise TypeError(f"{cls.__name__} must declare capabilities: ClassVar[frozenset[Capability]]")
        if Capability.CUSTOM_ATTRIBUTES in capabilities and not issubclass(cls, CustomAttributes):
            raise TypeError(f"{cls.__name__} declares {Capability.CUSTOM_ATTRIBUTES} but is no CustomAttributes")

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
    async def delete_role(self, *, role_id: uuid.UUID, data: dict | None = None) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_permission(
        self, *, permission_id: uuid.UUID, data: dict | None = None
    ) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]: ...
