"""What a Keycloak event changed, in the terms of the canonical model's mapping.

``KeycloakEventReader`` reads one event of the realm's admin-event or user-event
store and says which change of the mapping table's "In" column it records
(``docs/keycloak.md#event-pull``): an organization updated, disabled or deleted,
a user updated, disabled or deleted, an organization member added or removed, an
API client role (a permission or a role) created, updated or deleted, a role's
composites set, a user put in or taken out of an organization's role group.

It names entities as Keycloak does (an organization alias, a Keycloak user id and
username, a client role name and the canonical id it carries): which canonical
row each one is, if any, is ``InboundSyncService.apply_keycloak_change``'s
decision. An event the backend's own admin client wrote (``authDetails.userId``
is its service account; the service account cannot read its own client's id,
and acts for that client alone) changes nothing: it is the push coming back.

A deleted organization or client role is named in its event by its Keycloak id
alone, so the reader keeps a ``RealmSnapshot`` of each one's alias or name,
brought up to date by every event it reads (the backend's own included) and read
afresh from the realm after each poll.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass, field

from opentelemetry import trace

from app.logbook import logbook
from app.services.adapters.keycloak import PERMISSION_ATTRIBUTE, ROLE_ATTRIBUTE
from app.services.keycloak_admin import EventStream, KeycloakAdminApi, KeycloakAdminError

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# A client role whose name has this shape is a permission (``<domain>:<action>``)
# unless it carries a canonical role id.
PERMISSION_NAME_SEPARATOR = ":"


class Ignored(enum.StrEnum):
    """Why an event changes nothing canonical."""

    OWN_PUSH = "written by the backend's own admin client"
    NOT_MAPPED = "a Keycloak change the canonical model does not take in"
    OTHER_CLIENT = "a role of a client other than the API client"
    UNKNOWN_ENTITY = "names a Keycloak object the pull has no record of"
    UNREADABLE = "the event does not have the shape Keycloak gives that change"


@dataclass(frozen=True)
class KeycloakAccount:
    """A Keycloak user: its id, and its username when the event or the realm gives it.

    ``admin_named`` is true when a realm administrator's change names the account:
    only then may a username that is a canonical user id stand for that user. A
    user's own event never does, whatever username the account carries.
    """

    id: str | None
    username: str | None
    admin_named: bool = True


@dataclass(frozen=True)
class ClientRoleRef:
    """An API client role as Keycloak names it, with the canonical id it carries, if any.

    ``is_permission``: it stands for a canonical permission, not a role (``_ref``).
    """

    name: str
    role_id: str | None
    permission_id: str | None
    is_permission: bool


def _ref(name: str, role_id: str | None = None, permission_id: str | None = None) -> ClientRoleRef:
    """A client role is a permission when it carries a permission id, a role when it carries a role id,
    and otherwise a permission when its name is ``<domain>:<action>``."""
    if permission_id is not None:
        kind = True
    elif role_id is not None:
        kind = False
    else:
        kind = PERMISSION_NAME_SEPARATOR in name
    return ClientRoleRef(name=name, role_id=role_id, permission_id=permission_id, is_permission=kind)


@dataclass(frozen=True)
class TenantUpdated:
    alias: str
    name: str | None
    domains: tuple[str, ...] | None
    enabled: bool | None


@dataclass(frozen=True)
class TenantDeleted:
    alias: str


@dataclass(frozen=True)
class UserUpdated:
    account: KeycloakAccount
    email: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    enabled: bool | None = None


@dataclass(frozen=True)
class UserDeleted:
    account: KeycloakAccount


@dataclass(frozen=True)
class MembershipChanged:
    alias: str
    account: KeycloakAccount
    added: bool


@dataclass(frozen=True)
class ClientRoleSaved:
    """A client role created (``previous`` is ``None``) or updated (``previous`` is what it was)."""

    name: str
    description: str | None
    previous: ClientRoleRef | None
    # The kind of the role: a permission or a role (``ClientRoleRef.is_permission``).
    current: ClientRoleRef


@dataclass(frozen=True)
class ClientRoleDeleted:
    role: ClientRoleRef


@dataclass(frozen=True)
class CompositesChanged:
    role: ClientRoleRef
    permissions: tuple[ClientRoleRef, ...]
    added: bool


@dataclass(frozen=True)
class AssignmentChanged:
    alias: str
    account: KeycloakAccount
    group_name: str
    # The canonical role id the organization group carries, if it still exists and carries one.
    role_id: str | None
    added: bool


KeycloakChange = (
    TenantUpdated
    | TenantDeleted
    | UserUpdated
    | UserDeleted
    | MembershipChanged
    | ClientRoleSaved
    | ClientRoleDeleted
    | CompositesChanged
    | AssignmentChanged
)


@dataclass(frozen=True)
class Reading:
    """What one event changes: ``change``, or why nothing (``ignored``)."""

    change: KeycloakChange | None = None
    ignored: Ignored | None = None


@dataclass
class RealmSnapshot:
    """The aliases of the realm's organizations and the API client's roles, by Keycloak id."""

    organizations: dict[str, str] = field(default_factory=dict)
    client_roles: dict[str, dict] = field(default_factory=dict)


def _snapshot_role(snapshot: RealmSnapshot, keycloak_id: str) -> ClientRoleRef | None:
    held = snapshot.client_roles.get(keycloak_id)
    if not isinstance(held, dict) or not isinstance(held.get("name"), str):
        return None
    return _ref(held["name"], held.get("role_id"), held.get("permission_id"))


def _snapshot_role_named(snapshot: RealmSnapshot, name: str) -> str | None:
    """The Keycloak id of the API client role of that name, if the snapshot has one."""
    for keycloak_id in snapshot.client_roles:
        ref = _snapshot_role(snapshot, keycloak_id)
        if ref is not None and ref.name == name:
            return keycloak_id
    return None


def _representation(event: dict) -> object:
    raw = event.get("representation")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _attribute(entry: dict, name: str) -> str | None:
    attributes = entry.get("attributes")
    values = attributes.get(name) if isinstance(attributes, dict) else None
    if isinstance(values, list) and len(values) == 1 and isinstance(values[0], str):
        try:
            return str(uuid.UUID(values[0]))
        except ValueError:
            return None
    return None


def _role_record(entry: dict) -> dict:
    return {
        "name": entry["name"],
        "role_id": _attribute(entry, ROLE_ATTRIBUTE),
        "permission_id": _attribute(entry, PERMISSION_ATTRIBUTE),
    }


def client_role_ref(entry: dict) -> ClientRoleRef:
    """An API client role as the Admin API lists it (``name`` and ``attributes``), as a ``ClientRoleRef``."""
    with tracer.start_as_current_span("keycloak_events.client_role_ref"):
        record = _role_record(entry)
        return _ref(record["name"], record["role_id"], record["permission_id"])


def role_attribute(entry: dict) -> str | None:
    """The canonical role id a client role or an organization group carries, if one."""
    with tracer.start_as_current_span("keycloak_events.role_attribute"):
        return _attribute(entry, ROLE_ATTRIBUTE)


def _enabled(details: object) -> bool | None:
    """``enabled`` as an admin event's details record a change of it, else ``None`` (unchanged)."""
    value = details.get("updated_enabled") if isinstance(details, dict) else None
    return {"true": True, "false": False}.get(value) if isinstance(value, str) else None


# User events that change what the canonical model holds of a user.
_PROFILE_EVENTS = frozenset({"UPDATE_PROFILE", "UPDATE_EMAIL", "VERIFY_EMAIL"})
_DELETE_EVENTS = frozenset({"DELETE_ACCOUNT"})
_DISABLE_EVENTS = frozenset({"USER_DISABLED_BY_PERMANENT_LOCKOUT"})
# The admin events whose objects the snapshot keeps.
_SNAPSHOT_RESOURCES = frozenset({"ORGANIZATION", "CLIENT_ROLE"})


class KeycloakEventReader:
    """Reads the realm's events as changes of the mapping table, through the admin client."""

    def __init__(self, api: KeycloakAdminApi) -> None:
        self._api = api

    async def read(self, stream: EventStream, event: dict, snapshot: RealmSnapshot) -> Reading:
        """The change ``event`` records, bringing ``snapshot`` up to date with it.

        Raises ``KeycloakAdminError`` when the realm must be read to say and cannot be.
        """
        with tracer.start_as_current_span("KeycloakEventReader.read") as span:
            span.set_attribute("keycloak.event_stream", stream.name)
            if stream is EventStream.USER:
                return await self._user_event(event)
            return await self._admin_event(event, snapshot)

    async def refresh(self, snapshot: RealmSnapshot) -> None:
        """Add the realm's organizations' aliases and the API client's roles, as they are now, to the snapshot."""
        with tracer.start_as_current_span("KeycloakEventReader.refresh"):
            organizations = await self._api.list_organizations()
            roles = await self._api.client_roles()
            # Merged, never replaced: one deleted since the last event read keeps its entry
            # until its delete event is read, which needs it.
            snapshot.organizations = {
                **snapshot.organizations,
                **{entry["id"]: entry["alias"] for entry in organizations if isinstance(entry.get("alias"), str)},
            }
            snapshot.client_roles = {
                **snapshot.client_roles,
                **{entry["id"]: _role_record(entry) for entry in roles if isinstance(entry.get("name"), str)},
            }

    # --- user events --------------------------------------------------------------

    async def _user_event(self, event: dict) -> Reading:
        if event.get("clientId") == self._api.settings.client_id:
            return Reading(ignored=Ignored.OWN_PUSH)
        kind = event.get("type")
        user_id = _string(event.get("userId"))
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        if kind not in _PROFILE_EVENTS | _DELETE_EVENTS | _DISABLE_EVENTS:
            return Reading(ignored=Ignored.NOT_MAPPED)
        if user_id is None:
            return Reading(ignored=Ignored.UNREADABLE)
        account = KeycloakAccount(id=user_id, username=_string(details.get("username")), admin_named=False)
        if kind in _DELETE_EVENTS:
            return Reading(change=UserDeleted(account))
        if kind in _DISABLE_EVENTS:
            return Reading(change=UserUpdated(account, enabled=False))
        # A profile event records what changed in its own terms: the account as it is now says it whole.
        user = await self._api.get_user(user_id)
        if user is None:
            return Reading(ignored=Ignored.UNKNOWN_ENTITY)
        # The user chose this email themselves: it is taken only once Keycloak has verified it,
        # so no one can put another person's address on a canonical user.
        verified = user.get("emailVerified") is True
        return Reading(
            change=UserUpdated(
                KeycloakAccount(id=user_id, username=_string(user.get("username")), admin_named=False),
                email=_string(user.get("email")) if verified else None,
                given_name=_string(user.get("firstName")),
                family_name=_string(user.get("lastName")),
            )
        )

    # --- admin events -------------------------------------------------------------

    async def _admin_event(self, event: dict, snapshot: RealmSnapshot) -> Reading:
        operation = event.get("operationType")
        resource = event.get("resourceType")
        path = event.get("resourcePath")
        if not isinstance(path, str) or not isinstance(operation, str) or not isinstance(resource, str):
            return Reading(ignored=Ignored.UNREADABLE)
        parts = path.split("/")
        representation = _representation(event)
        auth = event.get("authDetails")
        author = auth.get("userId") if isinstance(auth, dict) else None
        if author is not None and author == await self._api.service_account_id():
            # The snapshot follows the backend's own changes too; nothing else is read for them.
            if resource in _SNAPSHOT_RESOURCES:
                await self._admin_change(operation, resource, parts, representation, event, snapshot)
            return Reading(ignored=Ignored.OWN_PUSH)
        return await self._admin_change(operation, resource, parts, representation, event, snapshot)

    async def _admin_change(
        self,
        operation: str,
        resource: str,
        parts: list[str],
        representation: object,
        event: dict,
        snapshot: RealmSnapshot,
    ) -> Reading:
        if resource == "ORGANIZATION":
            return self._organization(operation, parts, representation, snapshot)
        if resource == "ORGANIZATION_MEMBERSHIP":
            return await self._membership(operation, parts, representation, event, snapshot)
        if resource == "ORGANIZATION_GROUP_MEMBERSHIP":
            return await self._group_membership(operation, parts, representation, snapshot)
        if resource == "USER":
            return self._user(operation, parts, representation, event)
        if resource == "CLIENT_ROLE":
            return await self._client_role(operation, parts, representation, snapshot)
        return Reading(ignored=Ignored.NOT_MAPPED)

    @staticmethod
    def _organization(operation: str, parts: list[str], representation: object, snapshot: RealmSnapshot) -> Reading:
        if len(parts) != 2 or parts[0] != "organizations":
            return Reading(ignored=Ignored.NOT_MAPPED)
        organization_id = parts[1]
        if operation == "DELETE":
            alias = snapshot.organizations.pop(organization_id, None)
            return Reading(change=TenantDeleted(alias)) if alias else Reading(ignored=Ignored.UNKNOWN_ENTITY)
        if not isinstance(representation, dict):
            return Reading(ignored=Ignored.UNREADABLE)
        alias = _string(representation.get("alias")) or snapshot.organizations.get(organization_id)
        if alias is None:
            return Reading(ignored=Ignored.UNKNOWN_ENTITY)
        snapshot.organizations = {**snapshot.organizations, organization_id: alias}
        if operation != "UPDATE":
            # A new organization is not a tenant until the canonical side makes one.
            return Reading(ignored=Ignored.NOT_MAPPED)
        domains = representation.get("domains")
        names = (
            tuple(entry["name"] for entry in domains if isinstance(entry, dict) and isinstance(entry.get("name"), str))
            if isinstance(domains, list)
            else None
        )
        enabled = representation.get("enabled")
        return Reading(
            change=TenantUpdated(
                alias=alias,
                name=_string(representation.get("name")),
                domains=names,
                enabled=enabled if isinstance(enabled, bool) else None,
            )
        )

    async def _alias(self, organization_id: str, representation: object, snapshot: RealmSnapshot) -> str | None:
        """The organization's alias: from the event, the snapshot, or the realm."""
        if isinstance(representation, dict) and representation.get("id") == organization_id:
            alias = _string(representation.get("alias"))
            if alias is not None:
                return alias
        if organization_id in snapshot.organizations:
            return snapshot.organizations[organization_id]
        organization = await self._api.get_organization(organization_id)
        alias = _string(organization.get("alias")) if organization is not None else None
        if alias is not None:
            snapshot.organizations = {**snapshot.organizations, organization_id: alias}
        return alias

    async def _membership(
        self, operation: str, parts: list[str], representation: object, event: dict, snapshot: RealmSnapshot
    ) -> Reading:
        if (
            len(parts) < 3
            or parts[0] != "organizations"
            or parts[2] != "members"
            or operation not in ("CREATE", "DELETE")
        ):
            return Reading(ignored=Ignored.NOT_MAPPED)
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        username = _string(details.get("username"))
        if operation == "DELETE":
            if len(parts) != 4:
                return Reading(ignored=Ignored.UNREADABLE)
            account = KeycloakAccount(id=parts[3], username=username)
        else:
            if username is None:
                return Reading(ignored=Ignored.UNREADABLE)
            # The path of an added member does not name them: the username in the details does.
            found = await self._api.find_user(username)
            account = KeycloakAccount(id=found["id"] if found is not None else None, username=username)
        alias = await self._alias(parts[1], representation, snapshot)
        if alias is None:
            return Reading(ignored=Ignored.UNKNOWN_ENTITY)
        return Reading(change=MembershipChanged(alias=alias, account=account, added=operation == "CREATE"))

    async def _group_membership(
        self, operation: str, parts: list[str], representation: object, snapshot: RealmSnapshot
    ) -> Reading:
        shape = len(parts) == 6 and parts[0] == "organizations" and parts[2] == "groups" and parts[4] == "members"
        if not shape or operation not in ("CREATE", "DELETE"):
            return Reading(ignored=Ignored.NOT_MAPPED)
        organization_id, group_id, user_id = parts[1], parts[3], parts[5]
        name = _string(representation.get("name")) if isinstance(representation, dict) else None
        alias = await self._alias(organization_id, None, snapshot)
        if alias is None:
            return Reading(ignored=Ignored.UNKNOWN_ENTITY)
        role_id = None
        try:
            groups = [
                entry for entry in await self._api.organization_groups(organization_id) if entry["id"] == group_id
            ]
        except KeycloakAdminError as exc:
            # The organization is gone since: the event's own group name is all there is.
            if exc.status != 404:
                raise
            groups = []
        if groups:
            role_id = _attribute(groups[0], ROLE_ATTRIBUTE)
            name = _string(groups[0].get("name")) or name
        if name is None:
            return Reading(ignored=Ignored.UNREADABLE)
        return Reading(
            change=AssignmentChanged(
                alias=alias,
                account=KeycloakAccount(id=user_id, username=None),
                group_name=name,
                role_id=role_id,
                added=operation == "CREATE",
            )
        )

    @staticmethod
    def _user(operation: str, parts: list[str], representation: object, event: dict) -> Reading:
        if len(parts) != 2 or parts[0] != "users":
            return Reading(ignored=Ignored.NOT_MAPPED)
        rep = representation if isinstance(representation, dict) else {}
        account = KeycloakAccount(id=parts[1], username=_string(rep.get("username")))
        if operation == "DELETE":
            return Reading(change=UserDeleted(account))
        if operation != "UPDATE":
            # A user created in Keycloak is provisioned at their first sign-in.
            return Reading(ignored=Ignored.NOT_MAPPED)
        return Reading(
            change=UserUpdated(
                account,
                email=_string(rep.get("email")),
                given_name=rep["firstName"] if isinstance(rep.get("firstName"), str) else None,
                family_name=rep["lastName"] if isinstance(rep.get("lastName"), str) else None,
                enabled=_enabled(event.get("details")),
            )
        )

    async def _client_role(
        self, operation: str, parts: list[str], representation: object, snapshot: RealmSnapshot
    ) -> Reading:
        """``clients/<id>/roles/<name>[/composites]`` or ``roles-by-id/<id>[/composites]``."""
        composites = parts[-1] == "composites"
        target = parts[:-1] if composites else parts
        keycloak_id: str | None
        if len(target) == 2 and target[0] == "roles-by-id":
            keycloak_id = target[1]
            if keycloak_id not in snapshot.client_roles and not (operation == "CREATE" and not composites):
                # Not a role of the API client the pull has seen: another client's, or a realm role.
                return Reading(ignored=Ignored.UNKNOWN_ENTITY)
        elif len(target) == 4 and target[0] == "clients" and target[2] == "roles":
            if target[1] != await self._api.api_client_uuid():
                return Reading(ignored=Ignored.OTHER_CLIENT)
            keycloak_id = _snapshot_role_named(snapshot, target[3])
        else:
            return Reading(ignored=Ignored.NOT_MAPPED)
        if composites:
            return self._composites(operation, keycloak_id, representation, snapshot)
        if operation == "CREATE":
            return self._role_created(representation, snapshot)
        if keycloak_id is None or _snapshot_role(snapshot, keycloak_id) is None:
            return Reading(ignored=Ignored.UNKNOWN_ENTITY)
        previous = _snapshot_role(snapshot, keycloak_id)
        if operation == "DELETE":
            snapshot.client_roles = {key: value for key, value in snapshot.client_roles.items() if key != keycloak_id}
            return Reading(change=ClientRoleDeleted(previous))
        if operation != "UPDATE" or not isinstance(representation, dict):
            return Reading(ignored=Ignored.UNREADABLE)
        name = _string(representation.get("name")) or previous.name
        record = {**snapshot.client_roles[keycloak_id], "name": name}
        snapshot.client_roles = {**snapshot.client_roles, keycloak_id: record}
        current = _ref(name, previous.role_id, previous.permission_id)
        description = representation.get("description")
        return Reading(
            change=ClientRoleSaved(
                name=name,
                description=description if isinstance(description, str) else None,
                previous=previous,
                current=current,
            )
        )

    @staticmethod
    def _role_created(representation: object, snapshot: RealmSnapshot) -> Reading:
        if not isinstance(representation, dict):
            return Reading(ignored=Ignored.UNREADABLE)
        keycloak_id, name = _string(representation.get("id")), _string(representation.get("name"))
        if keycloak_id is None or name is None:
            return Reading(ignored=Ignored.UNREADABLE)
        record = _role_record({**representation, "name": name})
        snapshot.client_roles = {**snapshot.client_roles, keycloak_id: record}
        current = _snapshot_role(snapshot, keycloak_id)
        description = representation.get("description")
        return Reading(
            change=ClientRoleSaved(
                name=name,
                description=description if isinstance(description, str) else None,
                previous=None,
                current=current,
            )
        )

    @staticmethod
    def _composites(
        operation: str, keycloak_id: str | None, representation: object, snapshot: RealmSnapshot
    ) -> Reading:
        role = _snapshot_role(snapshot, keycloak_id) if keycloak_id is not None else None
        if role is None:
            return Reading(ignored=Ignored.UNKNOWN_ENTITY)
        if operation not in ("CREATE", "DELETE") or not isinstance(representation, list):
            return Reading(ignored=Ignored.UNREADABLE)
        permissions = tuple(
            ref
            for ref in (
                _snapshot_role(snapshot, entry["id"])
                for entry in representation
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            )
            if ref is not None
        )
        return Reading(change=CompositesChanged(role=role, permissions=permissions, added=operation == "CREATE"))
