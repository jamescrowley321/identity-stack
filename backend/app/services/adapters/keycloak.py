"""KeycloakSyncAdapter — pushes canonical tenants and users to a Keycloak realm.

Through ``KeycloakAdminApi`` (the Admin REST API, one service account):

- ``sync_tenant`` creates or updates the tenant's **organization**. Its alias is
  the canonical tenant id, which Keycloak never lets change; its name is the
  tenant's name, and its domains the tenant's ``self_provisioning_domains`` when
  given. An update sends only those fields, so the organization's attributes
  (the realm admin's ``type:<type client>`` grant markers) and, when no domains
  are given, its domains stay as they are.
- ``delete_tenant`` deletes that organization; one not there is deleted.
- ``sync_user`` creates or updates the user whose **username is the canonical
  user id**, with the canonical email, enabled while the status is ``active`` or
  ``provisioned`` and disabled when ``inactive``. With ``left_tenant_id`` in the
  data (``UserService.remove_user_from_tenant``) it also ends the user's
  membership of that tenant's organization.
- ``delete_user`` deletes that user; one not there is deleted.
- ``sync_role_assignment`` adds the user to the tenant's organization: holding a
  role in a tenant is being its member. ``delete_role_assignment`` calls nothing,
  because another role may still hold the membership.
- Roles and permissions stay canonical: their methods call nothing, as
  ``OrySyncAdapter``'s do.

The adapter manages only the users it created. A Keycloak user who signed up
through the login page has another username and is not taken over by email: a
push for an email such a user holds is refused by Keycloak (409) and returned as
an error.

Every failure is ``Error(SyncError)`` with ``context["status"]``, the HTTP status
or ``None``; nothing raises.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from expression import Error, Ok, Result
from opentelemetry import trace

from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.keycloak_admin import KeycloakAdminApi, KeycloakAdminError

tracer = trace.get_tracer(__name__)

# Canonical user status -> Keycloak ``enabled``.
_ENABLED: dict[str, bool] = {"active": True, "provisioned": True, "inactive": False}


class _Refused(Exception):
    """A push the adapter refuses before or between calls. Internal: becomes ``Error(SyncError)``."""


def _error(operation: str, message: str, context: dict, status: int | None) -> Error[None, SyncError]:
    return Error(SyncError(message=message, operation=operation, context={**context, "status": status}))


async def _run(operation: str, context: dict, push: Callable[[], Awaitable[None]]) -> Result[None, SyncError]:
    try:
        await push()
    except KeycloakAdminError as exc:
        return _error(operation, str(exc), context, exc.status)
    except _Refused as exc:
        return _error(operation, str(exc), context, None)
    return Ok(None)


def _tenant_uuid(value: object) -> str:
    """``value`` as a canonical tenant id, in its canonical spelling. Anything else is refused."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise _Refused("left_tenant_id is not a tenant id") from None


class KeycloakSyncAdapter(IdentityProviderAdapter):
    """Syncs canonical tenants and users to Keycloak organizations and users. Roles stay canonical."""

    def __init__(self, api: KeycloakAdminApi) -> None:
        self._api = api

    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create or update the organization whose alias is ``tenant_id``.

        Expected data keys: name (required), self_provisioning_domains (optional).
        """
        context = {"tenant_id": str(tenant_id)}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return _error("sync_tenant", "Missing required sync data: name is required", context, None)
        alias = str(tenant_id)
        representation: dict = {"name": name, "alias": alias}
        domains = data.get("self_provisioning_domains")
        if domains:
            representation["domains"] = [{"name": domain} for domain in domains]

        async def push() -> None:
            organization = await self._api.find_organization(alias)
            if organization is None:
                await self._api.create_organization(representation)
            else:
                await self._api.update_organization(organization["id"], representation)

        with tracer.start_as_current_span("keycloak.sync_tenant") as span:
            span.set_attribute("tenant.id", alias)
            return await _run("sync_tenant", context, push)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        alias = str(tenant_id)

        async def push() -> None:
            organization = await self._api.find_organization(alias)
            if organization is not None:
                await self._api.delete_organization(organization["id"])

        with tracer.start_as_current_span("keycloak.delete_tenant") as span:
            span.set_attribute("tenant.id", alias)
            return await _run("delete_tenant", {"tenant_id": alias}, push)

    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create or update the user whose username is ``user_id``.

        Expected data keys: email, status (required); left_tenant_id (optional):
        the tenant whose organization the user has left.
        """
        context = {"user_id": str(user_id)}
        email = data.get("email")
        status = data.get("status")
        if not email or not status:
            return _error("sync_user", "Missing required sync data: email and status are required", context, None)
        if status not in _ENABLED:
            return _error("sync_user", f"Unknown status '{status}': expected one of {list(_ENABLED)}", context, None)
        username = str(user_id)
        left = data.get("left_tenant_id")
        try:
            left_alias = None if left is None else _tenant_uuid(left)
        except _Refused as exc:
            return _error("sync_user", str(exc), context, None)

        async def push() -> None:
            user = await self._api.find_user(username)
            if user is None:
                # A user created now belongs to no organization, so there is none to leave.
                await self._api.create_user({"username": username, "email": email, "enabled": _ENABLED[status]})
                return
            await self._api.update_user(user["id"], {"email": email, "enabled": _ENABLED[status]})
            if left_alias is not None:
                organization = await self._api.find_organization(left_alias)
                if organization is not None:
                    await self._api.remove_member(organization["id"], user["id"])

        with tracer.start_as_current_span("keycloak.sync_user") as span:
            span.set_attribute("user.id", username)
            return await _run("sync_user", context, push)

    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]:
        username = str(user_id)

        async def push() -> None:
            user = await self._api.find_user(username)
            if user is not None:
                await self._api.delete_user(user["id"])

        with tracer.start_as_current_span("keycloak.delete_user") as span:
            span.set_attribute("user.id", username)
            return await _run("delete_user", {"user_id": username}, push)

    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]:
        """Add the user to the tenant's organization. Both must have been pushed already."""
        alias, username = str(tenant_id), str(user_id)

        async def push() -> None:
            organization = await self._api.find_organization(alias)
            if organization is None:
                raise _Refused("the tenant has no organization in Keycloak")
            user = await self._api.find_user(username)
            if user is None:
                raise _Refused("the user is not in Keycloak")
            await self._api.add_member(organization["id"], user["id"])

        with tracer.start_as_current_span("keycloak.sync_role_assignment") as span:
            span.set_attribute("user.id", username)
            span.set_attribute("tenant.id", alias)
            return await _run("sync_role_assignment", {"tenant_id": alias, "user_id": username}, push)

    async def delete_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]:
        # Another role may still hold the membership; leaving the tenant goes through sync_user.
        return Ok(None)

    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        # Roles are canonical-only.
        return Ok(None)

    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)

    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        # Permissions are canonical-only.
        return Ok(None)

    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)
