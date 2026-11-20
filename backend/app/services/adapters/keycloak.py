"""KeycloakSyncAdapter — pushes every canonical entity Keycloak can hold to a Keycloak realm.

Through ``KeycloakAdminApi`` (the Admin REST API, one service account), per the
mapping of ``docs/keycloak.md#admin-adapter``:

- **Tenant → organization.** ``sync_tenant`` creates or updates the tenant's
  organization: the one the tenant is linked to (``tenants.external_org_id`` of
  the ``keycloak`` provider), else the one whose alias is the canonical tenant
  id, which it creates when absent and then links the tenant to, so a first
  sign-in through it finds the tenant. Its name is the tenant's, its domains
  exactly the tenant's ``self_provisioning_domains`` when that key is given, and
  ``enabled`` follows the tenant's ``status`` when that key is given (``active``
  enabled, ``suspended`` disabled). The fields Keycloak resets when a PUT leaves
  them out (``enabled`` among them) are sent back as found; the domains, which it
  keeps, are sent only when given; an organization that already matches is not
  written. A tenant linked to another provider's organization is refused.
  ``delete_tenant`` deletes the organization; one not there is deleted.
- **User → user.** ``sync_user`` creates or updates the user whose username is
  the canonical id: email, ``firstName`` and ``lastName`` (from ``given_name``
  and ``family_name`` when given), ``enabled`` from the status. A new user is
  enabled unless ``inactive``; an existing one gets ``enabled`` only on a status
  change (``STATUS_CHANGED``), so a profile update keeps a realm admin's disable.
  A changed email is sent with ``emailVerified: false``. A user created in a
  tenant (``CREATED_IN_TENANT``) is linked to the account (``idp_links``,
  ``keycloak``, marked as the backend's link), so its first sign-in finds it.
  With ``left_tenant_id`` (``UserService.remove_user_from_tenant``) it ends the
  user's membership of that tenant's organization first, then pushes only a
  changed email. A status change or leaving a tenant never creates an account:
  a user Keycloak lacks is an error (``Refusal.USER_NOT_IN_KEYCLOAK``).
  ``delete_user`` deletes the account under the canonical id.
- **Permission → client role** ``<domain>:<action>`` of the API client, carrying
  the permission's id in the attribute ``PERMISSION_ATTRIBUTE``.
- **Role → composite client role** of the API client, named as the role (a
  tenant's own role ``<name>@<tenant id>``, so two tenants' roles never share a
  name), carrying the role's id in ``ROLE_ATTRIBUTE``, whose composites are
  exactly its permissions when ``permission_names`` is given.
- **Role assignment → organization group membership.** Each organization owns one
  group per role, named as the role's client role and carrying ``ROLE_ATTRIBUTE``;
  the group maps no role, so the membership records the assignment and grants
  nothing a token carries. ``sync_role_assignment`` makes the user a member of
  the tenant's organization and of that group, creating the group when absent.
  ``delete_role_assignment`` takes the user out of the group, and, when it was the
  user's last role in the tenant (``LAST_ROLE_IN_TENANT``), ends the organization
  membership, which takes the user out of every group of it.

A client role or group is found by the canonical id in its attribute, else by its
name when it carries no canonical id (the realm import's roles and groups, which
the adapter then stamps); one of that name carrying another id is refused. A
rename (``previous_name``) renames the role's groups in every organization, then
the client role, so a retry after a partial failure still finds what is left.
Nothing ever maps a client role to a user or group.

Every push is idempotent: each reads the state first and writes only what
differs, and a repeat writes nothing. A push that reads an organization and sends
it back (``sync_tenant``, ``set_tenant_attributes``) holds the organization's lock
(``KeycloakLinks.organization_lock``) from the read to the write, so a suspension
or a domain change another push makes in between is not undone. A realm admin's
change in Keycloak takes no lock: one landing between such a read and its write
can still be undone. Every push but a delete reaches the
Keycloak account the canonical user's ``keycloak`` link names when a sign-in or
the backend made it (``KeycloakLinks.linked_subject``; the backend's links are
the accounts the adapter created and those an operator's seed names) unless it
is a client's service account, and otherwise the account under the canonical
id. No email or name is pushed to an account whose username is not the
canonical id: its email and names are the account's own, except the names its
owner changed on their own profile (``OWN_PROFILE_OF``), which go to that account
only.

Every failure is ``Error(SyncError)`` with ``context["status"]``, the HTTP status
or ``None``, ``context[UNANSWERED]`` when Keycloak gave no readable answer, and
``context["refusal"]`` (a ``Refusal``) when the adapter refused the push itself.
A failure of the service's own database (``LocalDatabaseError``) has status
``None`` and is not ``UNANSWERED``: Keycloak was not asked.
"""

from __future__ import annotations

import enum
import json
import math
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import ClassVar, Protocol

from expression import Error, Ok, Result
from opentelemetry import trace

from app.middleware.providers import KEYCLOAK_MACHINE_CLIENT_PREFIX
from app.services.adapters.base import (
    CREATED_IN_TENANT,
    LAST_ROLE_IN_TENANT,
    OWN_PROFILE_OF,
    STATUS_CHANGED,
    UNANSWERED,
    AttributeValue,
    Capability,
    CustomAttributes,
    IdentityProviderAdapter,
    MachineClient,
    MachineClientRef,
    MachineCredentialPush,
    SyncError,
    TenantAttributes,
)
from app.services.keycloak_admin import KeycloakAdminApi, KeycloakAdminError

tracer = trace.get_tracer(__name__)

# The canonical provider row whose IdP links name Keycloak accounts and whose
# organizations tenants are linked to (scripts/seed_keycloak.py).
PROVIDER_NAME = "keycloak"

# The attributes that name the canonical entity a client role or group stands for.
ROLE_ATTRIBUTE = "identity-stack.role-id"
PERMISSION_ATTRIBUTE = "identity-stack.permission-id"
_CANONICAL_ATTRIBUTES = (ROLE_ATTRIBUTE, PERMISSION_ATTRIBUTE)

# The attributes a machine credential's client carries: the canonical credential
# and tenant it stands for, and the tenant's organization alias, which owns it.
CREDENTIAL_ATTRIBUTE = "identity-stack.credential-id"
TENANT_ATTRIBUTE = "identity-stack.tenant-id"
ORGANIZATION_ATTRIBUTE = "identity-stack.organization"
# The protocol mapper that puts the API client (``KEYCLOAK_AUDIENCE``) in a
# machine client's access tokens: the token middleware requires that audience.
_AUDIENCE_MAPPER = "oidc-audience-mapper"

# A Keycloak client's service account (``service-account-<client id>``): a client
# signs in with it, no person does, so an IdP link naming one is not followed.
SERVICE_ACCOUNT_PREFIX = "service-account-"

# Canonical user status -> Keycloak ``enabled``.
_ENABLED: dict[str, bool] = {"active": True, "provisioned": True, "inactive": False}
# Canonical tenant status -> organization ``enabled``.
_TENANT_ENABLED: dict[str, bool] = {"active": True, "suspended": False}


class Refusal(enum.StrEnum):
    """Why the adapter refused a push itself: a ``SyncError``'s message and ``context["refusal"]``."""

    MISSING_USER_DATA = "Missing required sync data: email and status are required"
    MISSING_NAME = "Missing required sync data: name is required"
    UNKNOWN_STATUS = "Unknown status"
    BAD_DOMAINS = "self_provisioning_domains is not a list of domain names"
    BAD_TENANT_ID = "a tenant id in the sync data is not a tenant id"
    TENANT_LINKED_ELSEWHERE = "the tenant is linked to an organization of another provider"
    NO_ORGANIZATION = "the tenant has no organization in Keycloak"
    USER_NOT_IN_KEYCLOAK = (
        "the user has no Keycloak account under its IdP link or the canonical user id: nothing was changed"
    )
    ROLE_NOT_IN_KEYCLOAK = "the role has no client role in Keycloak: push the role first"
    PERMISSION_NOT_IN_KEYCLOAK = "a permission of the role has no client role in Keycloak: push it first"
    NAME_HELD_BY_ANOTHER = "a client role or group of that name stands for another canonical entity"
    DUPLICATE_CANONICAL_ID = "several client roles or groups carry the same canonical id"
    GROUP_NOT_SHOWN = "Keycloak does not show the organization group it was asked to create"
    LINK_REFUSED = "the canonical link could not be written"
    NO_SERVICE_ACCOUNT = "Keycloak shows no service account for the machine client it registered"
    REGISTRATION_REFUSED = "Keycloak refused the machine client's registration token and still has its service account"


class KeycloakLinks(Protocol):
    """The canonical side: the ``keycloak`` provider's IdP links and tenant links."""

    async def linked_subject(self, user_id: uuid.UUID) -> str | None:
        """The Keycloak account id the user's ``keycloak`` link names, if a sign-in or the backend made it."""
        ...

    async def has_link(self, user_id: uuid.UUID) -> bool:
        """Whether the user has a ``keycloak`` link, whatever made it."""
        ...

    async def link_user(self, user_id: uuid.UUID, sub: str, email: str) -> None:
        """Link the user to the account the adapter created, marked as the backend's link."""
        ...

    async def organization_alias(self, tenant_id: uuid.UUID) -> str | None:
        """The alias of the ``keycloak`` organization the tenant is linked to, or ``None``."""
        ...

    async def linked_elsewhere(self, tenant_id: uuid.UUID) -> bool:
        """Whether the tenant is linked to an organization that is not a ``keycloak`` one."""
        ...

    async def link_organization(self, tenant_id: uuid.UUID, alias: str) -> None:
        """Record the organization ``alias`` as the tenant's (``external_org_id``, ``keycloak`` provider)."""
        ...

    async def role(self, role_id: uuid.UUID) -> tuple[str, uuid.UUID | None] | None:
        """The canonical role's name and its tenant (``None`` for a global role), or ``None`` when it is gone."""
        ...

    def organization_lock(self, alias: str) -> AbstractAsyncContextManager[None]:
        """Hold the lock of the organization ``alias``: one push at a time reads it and sends it back.

        Held across the service's workers. A failure taking it is ``LocalDatabaseError``.
        """
        ...


class _Refused(Exception):
    """A push the adapter refuses before or between calls. Internal: becomes ``Error(SyncError)``."""

    def __init__(self, refusal: Refusal, detail: str = "") -> None:
        super().__init__(f"{refusal.value}{': ' + detail if detail else ''}")
        self.refusal = refusal


class LinkRefused(Exception):
    """The canonical side refused a link write (the tenant is gone, linked to another organization)."""


class LocalDatabaseError(Exception):
    """The service's own database failed under a push (``KeycloakLinks``).

    Raised by what ``dependencies/keycloak_sync.py`` wires, in place of the
    database driver's or the pool's exception, so the adapter need not know them.
    Becomes ``Error(SyncError)`` with status ``None``: Keycloak was not asked.
    """


def _error(operation: str, message: str, context: dict, status: int | None) -> Error[None, SyncError]:
    return Error(SyncError(message=message, operation=operation, context={**context, "status": status}))


async def _run(operation: str, context: dict, push: Callable[[], Awaitable[None]]) -> Result[None, SyncError]:
    try:
        await push()
    except KeycloakAdminError as exc:
        unanswered = {UNANSWERED: True} if exc.status is None else {}
        return _error(operation, str(exc), {**context, **unanswered}, exc.status)
    except _Refused as exc:
        return _error(operation, str(exc), {**context, "refusal": exc.refusal}, None)
    except LinkRefused as exc:
        return _refused(operation, Refusal.LINK_REFUSED, context, str(exc))
    except LocalDatabaseError as exc:
        return _error(operation, str(exc), context, None)
    return Ok(None)


def _refused(operation: str, refusal: Refusal, context: dict, detail: str = "") -> Error[None, SyncError]:
    message = f"{refusal.value}{': ' + detail if detail else ''}"
    return _error(operation, message, {**context, "refusal": refusal}, None)


def _is_service_account(account: dict) -> bool:
    username = account.get("username")
    return bool(account.get("serviceAccountClientId")) or (
        isinstance(username, str) and username.startswith(SERVICE_ACCOUNT_PREFIX)
    )


def _tenant_uuid(value: object) -> str:
    """``value`` as a canonical tenant id, in its canonical spelling. Anything else is refused."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise _Refused(Refusal.BAD_TENANT_ID) from None


def _domain_names(value: object) -> list[str]:
    """``self_provisioning_domains`` as a list of names. Anything but a list of names is refused."""
    if not isinstance(value, list) or not all(isinstance(domain, str) and domain for domain in value):
        raise _Refused(Refusal.BAD_DOMAINS)
    return value


def _same_email(held: object, sent: str) -> bool:
    """Whether ``sent`` is the address Keycloak holds; Keycloak stores emails in lower case."""
    return isinstance(held, str) and held.lower() == sent.lower()


def role_client_name(name: str, tenant_id: object) -> str:
    """The client role (and group) name of a canonical role: its name, or ``<name>@<tenant id>`` for a tenant's own."""
    return name if tenant_id is None else f"{name}@{_tenant_uuid(tenant_id)}"


def _attributes(entry: dict) -> dict:
    attributes = entry.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _carries(entry: dict, attribute: str, canonical_id: str) -> bool:
    values = _attributes(entry).get(attribute)
    return isinstance(values, list) and canonical_id in values


def _stamped(entry: dict) -> bool:
    return any(_attributes(entry).get(attribute) for attribute in _CANONICAL_ATTRIBUTES)


def _find(entries: list[dict], attribute: str, canonical_id: str, names: list[str]) -> dict | None:
    """The entry carrying ``canonical_id``, else the unstamped one named first among ``names``.

    One of those names that carries another canonical id is refused: it stands
    for another canonical entity.
    """
    carrying = [entry for entry in entries if _carries(entry, attribute, canonical_id)]
    if len(carrying) > 1:
        raise _Refused(Refusal.DUPLICATE_CANONICAL_ID)
    if carrying:
        return carrying[0]
    for name in names:
        named = [entry for entry in entries if entry.get("name") == name]
        if named:
            if _stamped(named[0]):
                raise _Refused(Refusal.NAME_HELD_BY_ANOTHER, name)
            return named[0]
    return None


def _stamp(entry: dict, attribute: str, canonical_id: str) -> dict:
    """The entry's attributes with ``attribute`` set to ``canonical_id``."""
    return {**_attributes(entry), attribute: [canonical_id]}


def machine_client_id(credential_id: uuid.UUID) -> str:
    """The client id of a machine credential's client: the prefix the token binding knows, then the credential id."""
    return f"{KEYCLOAK_MACHINE_CLIENT_PREFIX}{credential_id}"


def _machine_client_representation(
    credential_id: uuid.UUID, tenant_id: uuid.UUID, name: str, alias: str, api: str
) -> dict:
    """A confidential client that signs in with the client-credentials grant only, owned by the tenant."""
    return {
        "clientId": machine_client_id(credential_id),
        "name": name,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "bearerOnly": False,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "frontchannelLogout": False,
        "fullScopeAllowed": False,
        "redirectUris": [],
        "webOrigins": [],
        "defaultClientScopes": [],
        "optionalClientScopes": [],
        "attributes": {
            CREDENTIAL_ATTRIBUTE: str(credential_id),
            TENANT_ATTRIBUTE: str(tenant_id),
            ORGANIZATION_ATTRIBUTE: alias,
        },
        "protocolMappers": [
            {
                "name": "API audience",
                "protocol": "openid-connect",
                "protocolMapper": _AUDIENCE_MAPPER,
                "config": {
                    "included.client.audience": api,
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                    "introspection.token.claim": "true",
                },
            }
        ],
    }


# A user's fields Keycloak keeps when a PUT leaves them out, which an attribute write never sends.
_KEPT_ON_A_USER_PUT = frozenset({"enabled", "emailVerified", "requiredActions"})

# An organization's fields Keycloak 26.8.2 resets when a PUT leaves them out (``enabled``
# to true): a write sends them as read (but those it changes), and of the rest only what
# it changes (the attributes, the domains).
_ORGANIZATION_RESET_ON_A_PUT = ("id", "name", "alias", "enabled", "description", "redirectUrl")


def _attribute_values(attributes: dict[str, AttributeValue], held: dict) -> dict:
    """``held`` with each of ``attributes`` set, as a JSON value; ``None`` removes it."""
    merged = dict(held)
    for key, value in attributes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = [json.dumps(value)]
    return merged


def _attribute_value(values: object) -> AttributeValue:
    """A single-valued attribute as the API's value: its JSON, or the text a realm admin typed.

    Text that decodes to no JSON value the API can answer with (``NaN``, ``Infinity``,
    an object, an array too deep to decode) is that text.
    """
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return None
    try:
        value = json.loads(values[0])
    except (ValueError, RecursionError):
        return values[0]
    if isinstance(value, float) and not math.isfinite(value):
        return values[0]
    return value if isinstance(value, str | int | float | bool) else values[0]


def _custom_attributes(entry: dict) -> dict[str, AttributeValue]:
    """The entry's single-valued attributes: a multi-valued one is no custom attribute."""
    decoded = {key: _attribute_value(values) for key, values in _attributes(entry).items()}
    return {key: value for key, value in decoded.items() if value is not None}


class KeycloakSyncAdapter(IdentityProviderAdapter, MachineCredentialPush, CustomAttributes):
    """Pushes canonical tenants, users, memberships, permissions, roles and role assignments to Keycloak.

    It also keeps each machine credential's client (``MachineCredentialPush``):
    created through client registration with ``create-client``, enabled and
    disabled through its service account (``manage-users``), and deleted with its
    registration access token. The admin client reads or changes no client
    through the Admin API (``keycloak/fixtures/README.md``).

    Custom attributes (``CustomAttributes``) are the account's user attributes and the
    tenant's organization attributes, each value one JSON string. The realm's user
    profile lets admins edit attributes it does not declare (``unmanagedAttributePolicy``
    ``ADMIN_EDIT``): the user cannot set them in the account console. Keycloak replaces
    a user's or an organization's attributes with the ones a PUT sends, and a user's
    profile fields with the ones it sends, so each write sends back what it read (but a
    user's status fields, which Keycloak keeps when a PUT leaves them out).
    """

    ends_memberships: ClassVar[bool] = True
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.MACHINE_CREDENTIALS, Capability.CUSTOM_ATTRIBUTES}
    )
    canonical_capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.MACHINE_CREDENTIALS})

    def __init__(self, api: KeycloakAdminApi, links: KeycloakLinks) -> None:
        self._api = api
        self._links = links

    # --- lookups ------------------------------------------------------------------

    async def _account(self, user_id: uuid.UUID) -> dict | None:
        """The canonical user's Keycloak account: the one their followed link names, else the one under their id.

        A linked account that is a client's service account is not followed.
        """
        subject = await self._links.linked_subject(user_id)
        if subject is not None:
            linked = await self._api.get_user(subject)
            if linked is not None and not _is_service_account(linked):
                return linked
        return await self._api.find_user(str(user_id))

    async def _alias(self, tenant_id: uuid.UUID) -> str:
        """The alias of the tenant's organization: the linked one, else the canonical tenant id."""
        return await self._links.organization_alias(tenant_id) or str(tenant_id)

    async def _organization(self, tenant_id: uuid.UUID) -> dict | None:
        return await self._api.find_organization(await self._alias(tenant_id))

    async def _role(self, role_id: uuid.UUID) -> dict | None:
        """The role's client role: the one carrying its id, else the unstamped one of its name and scope.

        The name and scope are the canonical role's own (``KeycloakLinks.role``),
        so a tenant's role never falls back to a global role of the same name.
        """
        return _find(await self._api.client_roles(), ROLE_ATTRIBUTE, str(role_id), await self._role_names(role_id))

    async def _role_names(self, role_id: uuid.UUID) -> list[str]:
        canonical = await self._links.role(role_id)
        return [] if canonical is None else [role_client_name(canonical[0], canonical[1])]

    async def _group(self, organization_id: str, role_id: uuid.UUID, names: list[str]) -> dict | None:
        return _find(await self._api.organization_groups(organization_id), ROLE_ATTRIBUTE, str(role_id), names)

    async def _role_organizations(self, tenant_id: object) -> list[dict]:
        """The organizations a role's groups can be in: the tenant's for a tenant's role, else every one."""
        if tenant_id is None:
            return await self._api.list_organizations()
        organization = await self._organization(uuid.UUID(_tenant_uuid(tenant_id)))
        return [] if organization is None else [organization]

    # --- tenants ------------------------------------------------------------------

    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create or update the tenant's organization, and link the tenant to one the adapter created.

        Expected data keys: name (required); self_provisioning_domains, status (optional).
        """
        context = {"tenant_id": str(tenant_id)}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return _refused("sync_tenant", Refusal.MISSING_NAME, context)
        wanted: dict = {"name": name}
        try:
            if "self_provisioning_domains" in data:
                wanted["domains"] = _domain_names(data["self_provisioning_domains"])
        except _Refused as exc:
            return _refused("sync_tenant", exc.refusal, context)
        if "status" in data:
            if data["status"] not in _TENANT_ENABLED:
                return _refused(
                    "sync_tenant", Refusal.UNKNOWN_STATUS, context, f"expected one of {list(_TENANT_ENABLED)}"
                )
            wanted["enabled"] = _TENANT_ENABLED[data["status"]]

        async def push() -> None:
            linked = await self._links.organization_alias(tenant_id)
            if linked is None and await self._links.linked_elsewhere(tenant_id):
                raise _Refused(Refusal.TENANT_LINKED_ELSEWHERE)
            alias = linked or str(tenant_id)
            async with self._links.organization_lock(alias):
                organization = await self._api.find_organization(alias)
                if organization is None:
                    representation = {"name": name, "alias": alias}
                    if "domains" in wanted:
                        representation["domains"] = [{"name": domain} for domain in wanted["domains"]]
                    if "enabled" in wanted:
                        representation["enabled"] = wanted["enabled"]
                    await self._api.create_organization(representation)
                else:
                    await self._update_organization(organization, wanted)
            if linked is None:
                await self._links.link_organization(tenant_id, alias)

        with tracer.start_as_current_span("keycloak.sync_tenant") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            return await _run("sync_tenant", context, push)

    async def _update_organization(self, found: dict, wanted: dict) -> None:
        """Send the organization back with the wanted fields replaced; nothing when it already matches.

        Keycloak 26.8.2 resets the fields of ``_ORGANIZATION_RESET_ON_A_PUT`` a PUT
        leaves out (``enabled`` to true, ``description`` and ``redirectUrl`` to
        nothing), so they are sent as found. The domains and ``attributes``, which
        it keeps, are sent only when wanted (domains) or never (attributes): sent as
        found, they would undo a change made since the read.
        """
        held_domains = sorted(entry.get("name") for entry in found.get("domains") or [] if isinstance(entry, dict))
        differs = found.get("name") != wanted["name"]
        differs |= "domains" in wanted and held_domains != sorted(wanted["domains"])
        differs |= "enabled" in wanted and found.get("enabled") is not wanted["enabled"]
        if not differs:
            return
        representation = {field: found[field] for field in _ORGANIZATION_RESET_ON_A_PUT if field in found}
        representation["name"] = wanted["name"]
        if "domains" in wanted:
            representation["domains"] = [{"name": domain} for domain in wanted["domains"]]
        if "enabled" in wanted:
            representation["enabled"] = wanted["enabled"]
        await self._api.update_organization(found["id"], representation)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        async def push() -> None:
            organization = await self._organization(tenant_id)
            if organization is not None:
                await self._api.delete_organization(organization["id"])

        with tracer.start_as_current_span("keycloak.delete_tenant") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            return await _run("delete_tenant", {"tenant_id": str(tenant_id)}, push)

    # --- custom attributes (CustomAttributes) ---------------------------------------

    async def _full_organization(self, tenant: str) -> dict | None:
        """The tenant's organization with its attributes, which a search by alias leaves out."""
        organization = await self._organization(uuid.UUID(_tenant_uuid(tenant)))
        return None if organization is None else await self._api.get_organization(organization["id"])

    async def user_attributes(self, *, subject: str) -> Result[dict[str, AttributeValue], SyncError]:
        found: dict[str, AttributeValue] = {}

        async def read() -> None:
            account = await self._api.get_user(subject)
            if account is not None:
                found.update(_custom_attributes(account))

        with tracer.start_as_current_span("keycloak.user_attributes"):
            return (await _run("user_attributes", {"subject": subject}, read)).map(lambda _: found)

    async def set_user_attribute(self, *, subject: str, key: str, value: AttributeValue) -> Result[None, SyncError]:
        async def push() -> None:
            account = await self._api.get_user(subject)
            if account is None:
                raise _Refused(Refusal.USER_NOT_IN_KEYCLOAK)
            attributes = _attribute_values({key: value}, _attributes(account))
            # The status fields are left out: Keycloak keeps what a PUT leaves out of them,
            # and sending them back as read would undo a disable made between the read
            # and the write.
            sent = {field: held for field, held in account.items() if field not in _KEPT_ON_A_USER_PUT}
            await self._api.update_user(account["id"], {**sent, "attributes": attributes})

        with tracer.start_as_current_span("keycloak.set_user_attribute"):
            return await _run("set_user_attribute", {"subject": subject}, push)

    async def tenant_attributes(self, *, tenant: str) -> Result[TenantAttributes | None, SyncError]:
        found: list[TenantAttributes] = []

        async def read() -> None:
            organization = await self._full_organization(tenant)
            if organization is not None:
                found.append(TenantAttributes(organization.get("name") or "", _custom_attributes(organization)))

        with tracer.start_as_current_span("keycloak.tenant_attributes"):
            return (await _run("tenant_attributes", {"tenant_id": tenant}, read)).map(
                lambda _: found[0] if found else None
            )

    async def set_tenant_attributes(
        self, *, tenant: str, attributes: dict[str, AttributeValue]
    ) -> Result[None, SyncError]:
        async def push() -> None:
            alias = await self._alias(uuid.UUID(_tenant_uuid(tenant)))
            # Locked from the read to the write: ``enabled`` goes back as read, so another
            # push's suspension in between would otherwise be undone.
            async with self._links.organization_lock(alias):
                organization = await self._full_organization(tenant)
                if organization is None:
                    raise _Refused(Refusal.NO_ORGANIZATION)
                # Keycloak keeps the domains a PUT leaves out, but resets ``enabled`` and the
                # other fields of _ORGANIZATION_RESET_ON_A_PUT: those go back as read.
                sent = {field: organization[field] for field in _ORGANIZATION_RESET_ON_A_PUT if field in organization}
                held = _attributes(organization)
                await self._api.update_organization(
                    organization["id"], {**sent, "attributes": _attribute_values(attributes, held)}
                )

        with tracer.start_as_current_span("keycloak.set_tenant_attributes"):
            return await _run("set_tenant_attributes", {"tenant_id": tenant}, push)

    # --- users --------------------------------------------------------------------

    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create or update the user whose username is ``user_id``.

        Expected data keys: email, status (required); given_name, family_name,
        ``CREATED_IN_TENANT``, ``STATUS_CHANGED``, left_tenant_id (optional). With
        ``OWN_PROFILE_OF`` (the owner's own account id) only the names are pushed, to
        that account, whether or not its username is the canonical id: the owner
        changed them.
        """
        context = {"user_id": str(user_id)}
        email = data.get("email")
        status = data.get("status")
        if not email or not status:
            return _refused("sync_user", Refusal.MISSING_USER_DATA, context)
        if status not in _ENABLED:
            return _refused("sync_user", Refusal.UNKNOWN_STATUS, context, f"expected one of {list(_ENABLED)}")
        username = str(user_id)
        status_changed = data.get(STATUS_CHANGED) is True
        created = data.get(CREATED_IN_TENANT) is not None
        left = data.get("left_tenant_id")
        try:
            left_tenant = None if left is None else uuid.UUID(_tenant_uuid(left))
        except _Refused as exc:
            return _refused("sync_user", exc.refusal, context)
        names = {
            key: data[field]
            for key, field in (("firstName", "given_name"), ("lastName", "family_name"))
            if isinstance(data.get(field), str) and data[field]
        }
        own = data.get(OWN_PROFILE_OF)

        async def push_own_names(subject: str) -> None:
            """The owner's own names, to their own account: sent back whole, but its status fields."""
            account = await self._api.get_user(subject)
            if account is None or _is_service_account(account):
                raise _Refused(Refusal.USER_NOT_IN_KEYCLOAK)
            # A blank name is not sent: the realm's user profile requires both, and an
            # account without one is asked for it again at its next sign-in.
            wanted = {key: value for key, value in names.items() if account.get(key) != value}
            if not wanted:
                return
            sent = {field: held for field, held in account.items() if field not in _KEPT_ON_A_USER_PUT}
            await self._api.update_user(account["id"], {**sent, **wanted})

        if isinstance(own, str) and own:
            with tracer.start_as_current_span("keycloak.sync_user") as span:
                span.set_attribute("user.id", username)
                return await _run("sync_user", context, lambda: push_own_names(own))

        async def push() -> None:
            user = await self._account(user_id)
            if user is None and (left_tenant is not None or status_changed):
                # Offboarding and a status change never provision an account: a
                # disabled account beside the user's real one would disable nothing.
                raise _Refused(Refusal.USER_NOT_IN_KEYCLOAK)
            if user is None:
                await self._api.create_user(
                    {"username": username, "email": email, "enabled": _ENABLED[status], **names}
                )
                if created:
                    await self._link_new_user(user_id, email)
                return
            # A linked account's email and names are its owner's own: never pushed.
            named = user.get("username") == username
            if created and named:
                # A create whose answer, or whose link write, was lost: link it now.
                await self._link_new_user(user_id, email)
            if left_tenant is not None:
                # Before the profile update: a refused update must not keep the user in the tenant.
                organization = await self._organization(left_tenant)
                if organization is not None:
                    await self._api.remove_member(organization["id"], user["id"])
            update: dict = {}
            if named and not _same_email(user.get("email"), email):
                update.update(email=email, emailVerified=False)
            if named and left_tenant is None:
                update.update({key: value for key, value in names.items() if user.get(key) != value})
            if status_changed and user.get("enabled") is not _ENABLED[status]:
                update["enabled"] = _ENABLED[status]
            if update:
                await self._api.update_user(user["id"], update)

        with tracer.start_as_current_span("keycloak.sync_user") as span:
            span.set_attribute("user.id", username)
            return await _run("sync_user", context, push)

    async def _link_new_user(self, user_id: uuid.UUID, email: str) -> None:
        """Link the canonical user to the account under its id, unless it already has a ``keycloak`` link."""
        if await self._links.has_link(user_id):
            return
        account = await self._api.find_user(str(user_id))
        if account is None:
            raise _Refused(Refusal.USER_NOT_IN_KEYCLOAK)
        await self._links.link_user(user_id, account["id"], email)

    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]:
        username = str(user_id)

        async def push() -> None:
            user = await self._api.find_user(username)
            if user is not None:
                await self._api.delete_user(user["id"])

        with tracer.start_as_current_span("keycloak.delete_user") as span:
            span.set_attribute("user.id", username)
            return await _run("delete_user", {"user_id": username}, push)

    # --- permissions --------------------------------------------------------------

    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create or update the permission's client role.

        Expected data keys: name (required); description, previous_name (optional).
        """
        context = {"permission_id": str(permission_id)}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return _refused("sync_permission", Refusal.MISSING_NAME, context)
        names = [entry for entry in (data.get("previous_name"), name) if isinstance(entry, str) and entry]

        async def push() -> None:
            roles = await self._api.client_roles()
            found = _find(roles, PERMISSION_ATTRIBUTE, str(permission_id), names)
            await self._ensure_client_role(found, PERMISSION_ATTRIBUTE, str(permission_id), name, data)

        with tracer.start_as_current_span("keycloak.sync_permission") as span:
            span.set_attribute("permission.id", str(permission_id))
            return await _run("sync_permission", context, push)

    async def delete_permission(self, *, permission_id: uuid.UUID, data: dict | None = None) -> Result[None, SyncError]:
        """Delete the permission's client role; Keycloak drops it from every role's composites.

        Expected data keys: name (optional), for a client role that carries no canonical id.
        """
        names = [name for name in [(data or {}).get("name")] if isinstance(name, str) and name]

        async def push() -> None:
            found = _find(await self._api.client_roles(), PERMISSION_ATTRIBUTE, str(permission_id), names)
            if found is not None:
                await self._api.delete_role(found["id"])

        with tracer.start_as_current_span("keycloak.delete_permission") as span:
            span.set_attribute("permission.id", str(permission_id))
            return await _run("delete_permission", {"permission_id": str(permission_id)}, push)

    async def _ensure_client_role(
        self, found: dict | None, attribute: str, canonical_id: str, name: str, data: dict
    ) -> None:
        """Create the client role, or bring the one found to its name, description and canonical id."""
        description = data.get("description") if isinstance(data.get("description"), str) else None
        if found is None:
            representation: dict = {"name": name, "attributes": {attribute: [canonical_id]}}
            if description is not None:
                representation["description"] = description
            await self._api.create_client_role(representation)
            return
        wanted = {
            "name": name,
            "description": found.get("description", "") if description is None else description,
            "attributes": _stamp(found, attribute, canonical_id),
        }
        held = {
            "name": found.get("name"),
            "description": found.get("description", ""),
            "attributes": _attributes(found),
        }
        if held != wanted:
            await self._api.update_role(found["id"], wanted)

    # --- roles --------------------------------------------------------------------

    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create or update the role's composite client role, and set its composites to its permissions.

        Expected data keys: name (required); description, tenant_id, previous_name,
        permission_names (optional). Without ``permission_names`` the composites
        stay as they are.
        """
        context = {"role_id": str(role_id)}
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return _refused("sync_role", Refusal.MISSING_NAME, context)
        try:
            client_name = role_client_name(name, data.get("tenant_id"))
            previous = data.get("previous_name")
            names = (
                [role_client_name(previous, data.get("tenant_id"))] if isinstance(previous, str) and previous else []
            )
        except _Refused as exc:
            return _refused("sync_role", exc.refusal, context)
        names.append(client_name)
        permission_names = data.get("permission_names")

        async def push() -> None:
            roles = await self._api.client_roles()
            found = _find(roles, ROLE_ATTRIBUTE, str(role_id), names)
            if found is not None and found.get("name") != client_name:
                if not _carries(found, ROLE_ATTRIBUTE, str(role_id)):
                    # Stamped first, under its old name: a retry after a partial rename finds it by id.
                    stamped = {**found, "attributes": _stamp(found, ROLE_ATTRIBUTE, str(role_id))}
                    await self._api.update_role(
                        found["id"],
                        {key: stamped[key] for key in ("name", "attributes")}
                        | {"description": found.get("description", "")},
                    )
                # Groups next: until the client role is renamed, a retry still finds them by its old name.
                await self._rename_groups(role_id, found["name"], client_name, data.get("tenant_id"))
            await self._ensure_client_role(found, ROLE_ATTRIBUTE, str(role_id), client_name, data)
            if isinstance(permission_names, list):
                roles = await self._api.client_roles()
                role = _find(roles, ROLE_ATTRIBUTE, str(role_id), [])
                if role is None:
                    raise _Refused(Refusal.ROLE_NOT_IN_KEYCLOAK)
                await self._set_composites(role["id"], roles, permission_names)

        with tracer.start_as_current_span("keycloak.sync_role") as span:
            span.set_attribute("role.id", str(role_id))
            return await _run("sync_role", context, push)

    async def _set_composites(self, role_id: str, roles: list[dict], permission_names: list) -> None:
        """Make the role's composites exactly the client roles of ``permission_names``."""
        wanted: set[str] = set()
        for permission_name in permission_names:
            named = [entry for entry in roles if entry.get("name") == permission_name]
            if not named or _attributes(named[0]).get(ROLE_ATTRIBUTE):
                raise _Refused(Refusal.PERMISSION_NOT_IN_KEYCLOAK, str(permission_name))
            wanted.add(named[0]["id"])
        held = {entry["id"] for entry in await self._api.role_composites(role_id)}
        if wanted - held:
            await self._api.add_composites(role_id, sorted(wanted - held))
        if held - wanted:
            await self._api.remove_composites(role_id, sorted(held - wanted))

    async def _rename_groups(self, role_id: uuid.UUID, old: str, new: str, tenant_id: object) -> None:
        for organization in await self._role_organizations(tenant_id):
            group = await self._group(organization["id"], role_id, [old])
            if group is not None and group.get("name") != new:
                await self._api.update_organization_group(
                    organization["id"],
                    group["id"],
                    {"name": new, "attributes": _stamp(group, ROLE_ATTRIBUTE, str(role_id))},
                )

    async def delete_role(self, *, role_id: uuid.UUID, data: dict | None = None) -> Result[None, SyncError]:
        """Delete the role's groups in every organization, then its client role.

        Expected data keys: name, tenant_id (optional), for a client role and
        groups that carry no canonical id.
        """
        data = data or {}
        context = {"role_id": str(role_id)}
        name = data.get("name")
        try:
            names = [role_client_name(name, data.get("tenant_id"))] if isinstance(name, str) and name else []
        except _Refused as exc:
            return _refused("delete_role", exc.refusal, context)

        async def push() -> None:
            found = _find(await self._api.client_roles(), ROLE_ATTRIBUTE, str(role_id), names)
            group_names = [found["name"]] if found is not None else names
            for organization in await self._role_organizations(data.get("tenant_id")):
                group = await self._group(organization["id"], role_id, group_names)
                if group is not None:
                    await self._api.delete_organization_group(organization["id"], group["id"])
            if found is not None:
                await self._api.delete_role(found["id"])

        with tracer.start_as_current_span("keycloak.delete_role") as span:
            span.set_attribute("role.id", str(role_id))
            return await _run("delete_role", context, push)

    # --- role assignments and memberships ----------------------------------------

    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]:
        """Make the user a member of the tenant's organization and of the role's group there.

        The tenant, the user and the role must have been pushed already. No data
        key is read: the role's name and scope come from the canonical store.
        """

        async def push() -> None:
            organization = await self._organization(tenant_id)
            if organization is None:
                raise _Refused(Refusal.NO_ORGANIZATION)
            user = await self._account(user_id)
            if user is None:
                raise _Refused(Refusal.USER_NOT_IN_KEYCLOAK)
            role = await self._role(role_id)
            if role is None:
                raise _Refused(Refusal.ROLE_NOT_IN_KEYCLOAK)
            await self._api.add_member(organization["id"], user["id"])
            group = await self._ensure_group(organization["id"], role_id, role["name"])
            await self._api.add_group_member(organization["id"], group["id"], user["id"])

        with tracer.start_as_current_span("keycloak.sync_role_assignment") as span:
            span.set_attribute("user.id", str(user_id))
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("role.id", str(role_id))
            context = {"tenant_id": str(tenant_id), "user_id": str(user_id), "role_id": str(role_id)}
            return await _run("sync_role_assignment", context, push)

    async def _ensure_group(self, organization_id: str, role_id: uuid.UUID, name: str) -> dict:
        """The organization's group for the role, created or stamped with the role's id when needed."""
        group = await self._group(organization_id, role_id, [name])
        attributes = {ROLE_ATTRIBUTE: [str(role_id)]}
        if group is None:
            try:
                await self._api.create_organization_group(organization_id, {"name": name, "attributes": attributes})
            except KeycloakAdminError as exc:
                # A concurrent assignment of the same role created it first: found below.
                if exc.status != 409:
                    raise
        elif not _carries(group, ROLE_ATTRIBUTE, str(role_id)) or group.get("name") != name:
            stamped = {"name": name, "attributes": _stamp(group, ROLE_ATTRIBUTE, str(role_id))}
            await self._api.update_organization_group(organization_id, group["id"], stamped)
        else:
            return group
        group = await self._group(organization_id, role_id, [name])
        if group is None:
            raise _Refused(Refusal.GROUP_NOT_SHOWN)
        if not _carries(group, ROLE_ATTRIBUTE, str(role_id)):
            stamped = {"name": name, "attributes": _stamp(group, ROLE_ATTRIBUTE, str(role_id))}
            await self._api.update_organization_group(organization_id, group["id"], stamped)
        return group

    async def delete_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]:
        """Take the user out of the role's group; end the membership when it was their last role there.

        With ``LAST_ROLE_IN_TENANT: True`` the organization membership is ended,
        which takes the user out of every group of the organization. A tenant not
        in Keycloak has nothing to end; a user Keycloak lacks is an error
        (``Refusal.USER_NOT_IN_KEYCLOAK``).
        """
        data = data or {}

        async def push() -> None:
            organization = await self._organization(tenant_id)
            if organization is None:
                return
            user = await self._account(user_id)
            if user is None:
                raise _Refused(Refusal.USER_NOT_IN_KEYCLOAK)
            if data.get(LAST_ROLE_IN_TENANT) is True:
                await self._api.remove_member(organization["id"], user["id"])
                return
            role = await self._role(role_id)
            names = [role["name"]] if role is not None else await self._role_names(role_id)
            group = await self._group(organization["id"], role_id, names)
            if group is not None:
                await self._api.remove_group_member(organization["id"], group["id"], user["id"])

        with tracer.start_as_current_span("keycloak.delete_role_assignment") as span:
            span.set_attribute("user.id", str(user_id))
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("role.id", str(role_id))
            context = {"tenant_id": str(tenant_id), "user_id": str(user_id), "role_id": str(role_id)}
            return await _run("delete_role_assignment", context, push)

    # --- machine credentials ------------------------------------------------------

    async def create_machine_client(
        self, *, credential_id: uuid.UUID, tenant_id: uuid.UUID, name: str
    ) -> Result[MachineClient, SyncError]:
        """Register the credential's client, owned by the tenant's organization, and read its service account.

        A tenant with no linked organization is refused (``Refusal.NO_ORGANIZATION``).
        A client whose service account Keycloak does not show is deleted again.
        """
        created: list[MachineClient] = []

        async def push() -> None:
            alias = await self._links.organization_alias(tenant_id)
            if alias is None:
                raise _Refused(Refusal.NO_ORGANIZATION)
            client = await self._api.register_client(
                _machine_client_representation(credential_id, tenant_id, name, alias, self._api.settings.api_client_id)
            )
            account = await self._api.find_user(f"{SERVICE_ACCOUNT_PREFIX}{client['clientId']}".lower())
            if account is None:
                await self._api.unregister_client(client["clientId"], client["registrationAccessToken"])
                raise _Refused(Refusal.NO_SERVICE_ACCOUNT)
            created.append(
                MachineClient(
                    client_id=client["clientId"],
                    external_id=client["id"],
                    subject=account["id"],
                    secret=client["secret"],
                    registration_token=client["registrationAccessToken"],
                )
            )

        with tracer.start_as_current_span("keycloak.create_machine_client") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("machine_credential.id", str(credential_id))
            context = {"tenant_id": str(tenant_id), "credential_id": str(credential_id)}
            result = await _run("create_machine_client", context, push)
            if result.is_error():
                return Error(result.error)
            return Ok(created[0])

    async def set_machine_client_enabled(self, *, client: MachineClientRef, enabled: bool) -> Result[None, SyncError]:
        """Enable or disable the client's service account: a disabled one is refused the grant."""

        async def push() -> None:
            account = await self._api.get_user(client.subject)
            if account is None or not _is_service_account(account):
                raise _Refused(Refusal.NO_SERVICE_ACCOUNT)
            if account.get("enabled") is not enabled:
                await self._api.update_user(client.subject, {"enabled": enabled})

        with tracer.start_as_current_span("keycloak.set_machine_client_enabled") as span:
            span.set_attribute("machine_client.id", client.client_id)
            return await _run("set_machine_client_enabled", {"client_id": client.client_id}, push)

    async def delete_machine_client(self, *, client: MachineClientRef) -> Result[None, SyncError]:
        """Delete the client with its registration token. One whose service account is gone is deleted already."""

        async def push() -> None:
            if await self._api.unregister_client(client.client_id, client.registration_token):
                return
            if await self._api.find_user(f"{SERVICE_ACCOUNT_PREFIX}{client.client_id}".lower()) is not None:
                raise _Refused(Refusal.REGISTRATION_REFUSED)

        with tracer.start_as_current_span("keycloak.delete_machine_client") as span:
            span.set_attribute("machine_client.id", client.client_id)
            return await _run("delete_machine_client", {"client_id": client.client_id}, push)
