"""KeycloakSyncAdapter — pushes canonical tenants and users to a Keycloak realm.

Through ``KeycloakAdminApi`` (the Admin REST API, one service account):

- ``sync_tenant`` creates or updates the tenant's **organization**. Its alias is
  the canonical tenant id, which Keycloak never lets change; its name is the
  tenant's name, and its domains exactly the tenant's ``self_provisioning_domains``
  when that key is given (``[]`` clears them). An update sends the organization
  as found with only those fields replaced, so its ``enabled``, description and
  redirect URL stay as a realm admin set them (Keycloak resets fields a PUT leaves
  out). Its attributes are never sent, so the realm admin's ``type:<type client>``
  grant markers stay, and when the key is absent its domains stay as they are.
- ``delete_tenant`` deletes that organization; one not there is deleted.
- ``sync_user`` creates or updates the user whose **username is the canonical
  user id**, with the canonical email. A new user is enabled while the status is
  ``active`` or ``provisioned`` and disabled when ``inactive``; an existing one
  gets ``enabled`` only on a status change (``STATUS_CHANGED``, from activate and
  deactivate), so a profile update keeps a realm admin's disable, and a profile
  update with the same email sends nothing. A changed email is sent with
  ``emailVerified: false``: the realm trusts a verified email to link identities
  across providers, and the new address has not been verified. With
  ``left_tenant_id`` in the data (``UserService.remove_user_from_tenant``) it
  ends the user's membership of that tenant's organization first (a user
  Keycloak lacks is not created, and is an error: ``USER_NOT_IN_KEYCLOAK``), so a refused
  profile update cannot keep the user in the tenant, and then pushes only a
  changed email: leaving a tenant does not send ``enabled``, so it cannot undo a
  realm admin's disable. A status sync (activate, deactivate) does send it, and,
  like leaving a tenant, does not create a user Keycloak lacks
  (``USER_NOT_IN_KEYCLOAK``).
- ``delete_user`` deletes that user; one not there is deleted.
- ``sync_role_assignment`` adds the user to the tenant's organization: holding a
  role in a tenant is being its member. ``delete_role_assignment`` ends that
  membership when ``RoleService`` marks the removed role as the user's last in the
  tenant (``LAST_ROLE_IN_TENANT``); otherwise another role still holds it and
  nothing is called. ``ends_memberships`` is set, so ``RoleService.delete_role``
  also sends that for each user the deleted role was the last role of.
- Ending a membership (last role, leaving a tenant) first takes the member's id
  out of the organization's ``type:<type client>`` grant markers, so a later
  membership does not bring back a grant the realm admin gave the earlier one.
  Other attributes are kept.
- Roles and permissions stay canonical: their methods call nothing, as
  ``OrySyncAdapter``'s do.

Every push but a delete reaches the Keycloak account the canonical user's
``keycloak`` IdP link names (``linked_subject``: only a link a sign-in made, never
one an admin typed in) unless it is a client's service account, and, when there
is none or Keycloak no longer has it, the account under the canonical id; one is
created only when there is neither. No email is pushed to a linked account, so a
profile update of a linked user sends nothing. Deletes reach only the account
under the canonical id.

Writes that send an organization back whole (a tenant update, a grant-marker
trim with the removal after it) run under ``organization_lock`` for the
organization's alias, so none undoes another.

A Keycloak user who signed up through the login page and has no IdP link has
another username and is not taken over by email: a push for an email such a user
holds is refused by Keycloak (409) and returned as an error.

Every Keycloak failure is ``Error(SyncError)`` with ``context["status"]``, the HTTP
status or ``None``, and ``context[UNANSWERED]`` when Keycloak gave no readable
answer. Only a failure of ``linked_subject`` or ``organization_lock`` (the
service's own database) raises.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import ClassVar

from expression import Error, Ok, Result
from opentelemetry import trace

from app.services.adapters.base import (
    LAST_ROLE_IN_TENANT,
    STATUS_CHANGED,
    UNANSWERED,
    IdentityProviderAdapter,
    SyncError,
)
from app.services.keycloak_admin import KeycloakAdminApi, KeycloakAdminError
from app.services.keycloak_directory import GRANT_ATTRIBUTE_PREFIX

tracer = trace.get_tracer(__name__)

# A membership removal or status change found no Keycloak account: none named by
# the user's IdP link and none under the canonical id. A user who signed up through
# the login page and was never linked may hold one under another username, so it
# is reported, not counted as done, and no account is created for it.
USER_NOT_IN_KEYCLOAK = (
    "the user has no Keycloak account under its IdP link or the canonical user id: nothing was changed"
)

# The canonical provider row whose IdP links name Keycloak accounts (seeded by
# scripts/seed_keycloak.py; a Keycloak principal's provider name, lower-cased).
PROVIDER_NAME = "keycloak"

# The Keycloak account id (the token's ``sub``) a canonical user's ``keycloak`` IdP
# link names, or ``None`` when the user has no such link.
LinkedSubject = Callable[[uuid.UUID], Awaitable[str | None]]


async def _no_link(_user_id: uuid.UUID) -> str | None:
    return None


# Held, per organization alias, while the adapter reads an organization and sends
# it back whole (a tenant update, a grant-marker trim), so two such writes never
# interleave and the later one undo the earlier. ``dependencies/identity.py``
# wires a Postgres advisory lock, held across workers.
OrganizationLock = Callable[[str], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def _unlocked(_alias: str) -> AsyncIterator[None]:
    yield


# A Keycloak client's service account (``service-account-<client id>``): a client
# signs in with it, no person does, so an IdP link naming one is not followed.
SERVICE_ACCOUNT_PREFIX = "service-account-"


def _is_service_account(account: dict) -> bool:
    username = account.get("username")
    return bool(account.get("serviceAccountClientId")) or (
        isinstance(username, str) and username.startswith(SERVICE_ACCOUNT_PREFIX)
    )


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
        unanswered = {UNANSWERED: True} if exc.status is None else {}
        return _error(operation, str(exc), {**context, **unanswered}, exc.status)
    except _Refused as exc:
        return _error(operation, str(exc), context, None)
    return Ok(None)


def _tenant_uuid(value: object) -> str:
    """``value`` as a canonical tenant id, in its canonical spelling. Anything else is refused."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise _Refused("left_tenant_id is not a tenant id") from None


def _domains(value: object) -> list[dict]:
    """``self_provisioning_domains`` as Keycloak organization domains. Anything but a list of names is refused."""
    if not isinstance(value, list) or not all(isinstance(domain, str) and domain for domain in value):
        raise _Refused("self_provisioning_domains is not a list of domain names")
    return [{"name": domain} for domain in value]


def _merged(found: dict, representation: dict) -> dict:
    """The organization as found, with the adapter's fields replaced, to PUT back.

    Keycloak 26.8.2 resets the fields a PUT leaves out (``enabled`` to true,
    ``description`` and ``redirectUrl`` to nothing), so they are sent as found.
    ``attributes`` (the realm admin's ``type:`` markers) are never sent: Keycloak
    keeps them when a PUT leaves them out.
    """
    kept = {key: value for key, value in found.items() if key != "attributes"}
    return {**kept, **representation}


def _without_member(attributes: dict, member_id: str) -> dict:
    """The organization's attributes with ``member_id`` taken out of every ``type:`` grant marker.

    A marker left with no member is dropped (Keycloak drops it too); every other
    attribute is kept as it is.
    """
    kept: dict = {}
    for name, value in attributes.items():
        if name.startswith(GRANT_ATTRIBUTE_PREFIX) and isinstance(value, list):
            value = [holder for holder in value if holder != member_id]
            if not value:
                continue
        kept[name] = value
    return kept


def _same_email(held: object, sent: str) -> bool:
    """Whether ``sent`` is the address Keycloak holds; Keycloak stores emails in lower case."""
    return isinstance(held, str) and held.lower() == sent.lower()


class KeycloakSyncAdapter(IdentityProviderAdapter):
    """Syncs canonical tenants and users to Keycloak organizations and users. Roles stay canonical."""

    ends_memberships: ClassVar[bool] = True

    def __init__(
        self,
        api: KeycloakAdminApi,
        *,
        linked_subject: LinkedSubject = _no_link,
        organization_lock: OrganizationLock = _unlocked,
    ) -> None:
        self._api = api
        self._linked_subject = linked_subject
        self._organization_lock = organization_lock

    async def _account(self, user_id: uuid.UUID) -> dict | None:
        """The canonical user's Keycloak account: the one their sign-in link names, else the one under the canonical id.

        A linked account that is a client's service account is not followed.
        """
        subject = await self._linked_subject(user_id)
        if subject is not None:
            linked = await self._api.get_user(subject)
            if linked is not None and not _is_service_account(linked):
                return linked
        return await self._api.find_user(str(user_id))

    async def _end_membership(self, alias: str, organization_id: str, member_id: str) -> None:
        """Take the member out of the organization's grant markers, then end the membership.

        Markers first: should the removal fail, the member is left without the
        grant rather than with it. Under the organization's lock: the organization
        is sent back whole, and another write between the read and the PUT would
        be undone by it.
        """
        async with self._organization_lock(alias):
            organization = await self._api.get_organization(organization_id)
            attributes = organization.get("attributes")
            if isinstance(attributes, dict):
                trimmed = _without_member(attributes, member_id)
                if trimmed != attributes:
                    kept = {key: value for key, value in organization.items() if key != "attributes"}
                    await self._api.update_organization(organization_id, {**kept, "attributes": trimmed})
            await self._api.remove_member(organization_id, member_id)

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
        if "self_provisioning_domains" in data:
            try:
                representation["domains"] = _domains(data["self_provisioning_domains"])
            except _Refused as exc:
                return _error("sync_tenant", str(exc), context, None)

        async def push() -> None:
            # Locked: the organization is sent back as found, so a write between would be undone.
            async with self._organization_lock(alias):
                organization = await self._api.find_organization(alias)
                if organization is None:
                    await self._api.create_organization(representation)
                else:
                    await self._api.update_organization(organization["id"], _merged(organization, representation))

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
        the tenant whose organization the user has left; STATUS_CHANGED (optional):
        ``True`` to push ``enabled`` for an existing user, which otherwise keeps
        the enable or disable a realm admin set.
        """
        context = {"user_id": str(user_id)}
        email = data.get("email")
        status = data.get("status")
        if not email or not status:
            return _error("sync_user", "Missing required sync data: email and status are required", context, None)
        if status not in _ENABLED:
            return _error("sync_user", f"Unknown status '{status}': expected one of {list(_ENABLED)}", context, None)
        username = str(user_id)
        status_changed = data.get(STATUS_CHANGED) is True
        left = data.get("left_tenant_id")
        try:
            left_alias = None if left is None else _tenant_uuid(left)
        except _Refused as exc:
            return _error("sync_user", str(exc), context, None)

        offboarding = left_alias is not None or status_changed

        async def push() -> None:
            # The account the user's sign-in link names, else the one under the canonical id.
            user = await self._account(user_id)
            if user is None and offboarding:
                # Offboarding and a status change never provision an account: a
                # disabled account beside the user's real one would disable nothing.
                raise _Refused(USER_NOT_IN_KEYCLOAK)
            if user is None:
                # No account under the link or the canonical id: the adapter creates one.
                await self._api.create_user({"username": username, "email": email, "enabled": _ENABLED[status]})
                return
            # A linked account's email is its owner's own: it is never pushed, and
            # a profile update of a linked user sends nothing.
            named = user.get("username") == username
            changed_email = named and not _same_email(user.get("email"), email)
            if left_alias is not None:
                # Before the profile update: a refused update must not keep the user in the tenant.
                organization = await self._api.find_organization(left_alias)
                if organization is not None:
                    await self._end_membership(left_alias, organization["id"], user["id"])
                # Leaving a tenant says nothing about the account's status: ``enabled``
                # is not sent, so a realm admin's disable stands. Only a new email is pushed.
                if changed_email:
                    await self._api.update_user(user["id"], {"email": email, "emailVerified": False})
                return
            # ``enabled`` only on a status change: a profile update does not undo a
            # realm admin's disable. With neither, there is nothing to push.
            update: dict = {}
            if changed_email:
                update["emailVerified"] = False
            if status_changed:
                update["enabled"] = _ENABLED[status]
            if update:
                await self._api.update_user(user["id"], {"email": email, **update} if named else update)

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
            user = await self._account(user_id)
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
        """End the user's membership of the tenant's organization when this was their last role there.

        Without ``LAST_ROLE_IN_TENANT: True`` in the data another role still holds
        the membership, and nothing is called. A tenant not in Keycloak, or a user
        who is not a member, has no membership to end. A user not in Keycloak under
        the canonical id is an error (``USER_NOT_IN_KEYCLOAK``).
        """
        if (data or {}).get(LAST_ROLE_IN_TENANT) is not True:
            return Ok(None)
        alias, username = str(tenant_id), str(user_id)

        async def push() -> None:
            organization = await self._api.find_organization(alias)
            if organization is None:
                return
            user = await self._account(user_id)
            if user is None:
                raise _Refused(USER_NOT_IN_KEYCLOAK)
            await self._end_membership(alias, organization["id"], user["id"])

        with tracer.start_as_current_span("keycloak.delete_role_assignment") as span:
            span.set_attribute("user.id", username)
            span.set_attribute("tenant.id", alias)
            return await _run("delete_role_assignment", {"tenant_id": alias, "user_id": username}, push)

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
