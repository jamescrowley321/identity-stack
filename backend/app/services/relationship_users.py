"""Who a relation names: the user directory that matches a relationship store's subjects.

Sharing a document and the FGA admin routes take a user identifier from the
caller and grant a relation to the user it names. The store keys a subject by the
id its reads use, so the identifier is looked up first, in the directory of the
same ids: Descope's users for Descope's store (Descope user ids), the canonical
users for OpenFGA (canonical user ids). Which directory goes with which store is
the store's registration (``app.dependencies.identity.RELATIONSHIP_STORES``).
"""

from __future__ import annotations

import uuid
from abc import abstractmethod
from dataclasses import dataclass
from typing import Protocol

from opentelemetry import trace

from app.outbound import UpstreamStatusError, UpstreamTransportError
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.user import UserRepository
from app.services.descope import DescopeManagementClient

tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class DirectoryUser:
    """A user the directory found: the id relations key on, and the tenants they belong to."""

    user_id: str
    tenant_ids: frozenset[str]


class UserDirectoryUnavailable(Exception):
    """The directory could not answer. ``upstream`` is for the log only."""

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        super().__init__(upstream)


class UserDirectory(Protocol):
    @abstractmethod
    async def find(self, identifier: str) -> DirectoryUser | None:
        """The user ``identifier`` names, or ``None``. Raises ``UserDirectoryUnavailable``."""
        ...


class DescopeUserDirectory:
    """Descope's users, by Descope user id or login id (``DescopeManagementClient.load_user``)."""

    def __init__(self, client: DescopeManagementClient) -> None:
        self._client = client

    async def find(self, identifier: str) -> DirectoryUser | None:
        with tracer.start_as_current_span(
            "DescopeUserDirectory.find", record_exception=False, set_status_on_exception=False
        ):
            try:
                user = await self._client.load_user(identifier)
            except UpstreamStatusError as exc:
                if exc.response.status_code == 404:
                    return None
                raise UserDirectoryUnavailable(f"HTTP {exc.response.status_code}") from exc
            except UpstreamTransportError as exc:
                raise UserDirectoryUnavailable(type(exc).__name__) from exc
        if not isinstance(user, dict) or not user.get("userId"):
            return None
        tenants = user.get("userTenants") or []
        return DirectoryUser(
            user_id=str(user["userId"]),
            tenant_ids=frozenset(t["tenantId"] for t in tenants if isinstance(t, dict) and t.get("tenantId")),
        )


class CanonicalUserDirectory:
    """The canonical users, by canonical user id or email; a user's tenants are those they hold a role in."""

    def __init__(self, users: UserRepository, assignments: UserTenantRoleRepository) -> None:
        self._users = users
        self._assignments = assignments

    async def find(self, identifier: str) -> DirectoryUser | None:
        with tracer.start_as_current_span(
            "CanonicalUserDirectory.find", record_exception=False, set_status_on_exception=False
        ):
            try:
                user_id = uuid.UUID(identifier)
            except ValueError:
                user = await self._users.get_by_email(identifier.strip())
            else:
                user = await self._users.get(user_id)
            if user is None:
                return None
            assignments = await self._assignments.list_by_user(user.id)
            return DirectoryUser(user_id=str(user.id), tenant_ids=frozenset(str(a.tenant_id) for a in assignments))
