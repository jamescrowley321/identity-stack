"""The Keycloak token model: one organization and one user type per token.

A realm token is for one **type client** (``aud``, for example ``member`` or
``tenant-admin``), was issued to that type's one **app client** (``azp``), names
one **organization** (the tenant) and carries the type client's **client roles**
(``resource_access[aud].roles``, the permissions). Realm roles are not used.

Client roles are realm-wide, not per organization, so every type is declared
one of two ways:

* **portable**: its token grants only the roles that are also on the type's
  portable list, and that list holds read-only permissions. What a portable
  token grants is the same in every organization its user belongs to.
* **organization-bound**: its token is believed only for a user whose one
  organization is the token's. This module says that the token is of such a
  type; the lookup that answers it is the caller's.

Everything here is a pure function of the configuration and the claims, and
every branch without one unambiguous answer refuses. The module imports nothing
from the application, and its branches are the ones identity-model#1050
(single-audience mode) and identity-model#1051 (Keycloak claim helpers) specify
for the library's FastAPI package.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

# The actions a permission may have and still cross organizations.
PORTABLE_ACTIONS = frozenset({"read", "list"})

_APP_CLIENTS = "KEYCLOAK_APP_CLIENTS"
_AUDIENCES = "KEYCLOAK_AUDIENCES"
_PORTABLE_TYPES = "KEYCLOAK_PORTABLE_TYPES"
_ORGANIZATION_BOUND_TYPES = "KEYCLOAK_ORGANIZATION_BOUND_TYPES"
_PORTABLE_PERMISSIONS = "KEYCLOAK_PORTABLE_PERMISSIONS"


class KeycloakModelError(ValueError):
    """The Keycloak settings do not describe a realm this backend can decide tokens for."""


@dataclass(frozen=True)
class KeycloakClientType:
    """One user type: its type client, its app client and how far its roles reach."""

    type_client: str
    app_client: str
    organization_bound: bool
    # The client roles a portable type grants. Empty for an organization-bound type.
    portable_permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class KeycloakTokenModel:
    """Every configured type, ordered by type client so that equal settings build equal models."""

    types: tuple[KeycloakClientType, ...]

    def type_for(self, audience: str) -> KeycloakClientType | None:
        for client_type in self.types:
            if client_type.type_client == audience:
                return client_type
        return None


@dataclass(frozen=True)
class TokenGrant:
    """What one bound token says: its organization and the permissions it carries there.

    ``organization_bound`` is true when the permissions hold only for a user
    whose one organization is this one, which the token cannot show.
    """

    organization: str
    permissions: tuple[str, ...]
    organization_bound: bool


def _pairs(raw: str, variable: str) -> list[tuple[str, str]]:
    """``a=b,c=d`` as pairs. An entry that is not exactly one non-empty pair raises."""
    pairs = []
    for entry in raw.split(","):
        if not entry.strip():
            continue
        name, separator, value = (part.strip() for part in entry.partition("="))
        if not separator or not name or not value or "=" in value:
            raise KeycloakModelError(f"{variable} entry {entry.strip()!r} is not <type client>=<value>")
        pairs.append((name, value))
    return pairs


def parse_app_clients(raw: str) -> dict[str, str]:
    """``KEYCLOAK_APP_CLIENTS``: ``<type client>=<app client>`` pairs, one app client per type."""
    app_clients: dict[str, str] = {}
    for type_client, app_client in _pairs(raw, _APP_CLIENTS):
        if type_client in app_clients:
            raise KeycloakModelError(f"{_APP_CLIENTS} names type client {type_client!r} more than once")
        app_clients[type_client] = app_client
    return app_clients


def parse_portable_permissions(raw: str) -> dict[str, frozenset[str]]:
    """``KEYCLOAK_PORTABLE_PERMISSIONS``: ``<type client>=<permission>`` pairs, gathered per type."""
    gathered: dict[str, set[str]] = {}
    for type_client, permission in _pairs(raw, _PORTABLE_PERMISSIONS):
        gathered.setdefault(type_client, set()).add(permission)
    return {type_client: frozenset(permissions) for type_client, permissions in gathered.items()}


def _is_portable(permission: str) -> bool:
    """True for ``<domain>:<action>`` with a non-empty domain and a read-only action."""
    domain, separator, action = permission.partition(":")
    return bool(domain) and bool(separator) and action in PORTABLE_ACTIONS


def token_model(
    *,
    audiences: Iterable[str],
    app_clients: Mapping[str, str],
    portable_types: Iterable[str],
    organization_bound_types: Iterable[str],
    portable_permissions: Mapping[str, frozenset[str]],
) -> KeycloakTokenModel:
    """The model for these settings, or ``KeycloakModelError`` when they leave a question open.

    Refused: a type client with no app client, an app client for a type that is
    not an audience, an app client that is itself an audience (its ID tokens
    would bind), a type declared neither portable nor organization-bound or
    declared both, a declared type that is not an audience, a portable type
    with no permission list, a listed permission whose action is not ``read``
    or ``list``, and a permission list for a type that is not portable.
    """
    type_clients = frozenset(audiences)
    portable = frozenset(portable_types)
    bound = frozenset(organization_bound_types)

    unmapped = sorted(type_clients - app_clients.keys())
    if unmapped:
        raise KeycloakModelError(
            f"{_APP_CLIENTS} must name the app client of every type client in {_AUDIENCES}: none for {unmapped}"
        )
    unknown_mapped = sorted(app_clients.keys() - type_clients)
    if unknown_mapped:
        raise KeycloakModelError(f"{_APP_CLIENTS} names type clients that are not in {_AUDIENCES}: {unknown_mapped}")
    also_audiences = sorted(type_client for type_client, app in app_clients.items() if app in type_clients)
    if also_audiences:
        raise KeycloakModelError(
            f"{_APP_CLIENTS} maps {also_audiences} to an app client that is itself in {_AUDIENCES}: "
            "an app client is never an accepted audience"
        )

    both = sorted(portable & bound)
    if both:
        raise KeycloakModelError(
            f"{_PORTABLE_TYPES} and {_ORGANIZATION_BOUND_TYPES} both declare {both}: a type is one or the other"
        )
    undeclared = sorted(type_clients - portable - bound)
    if undeclared:
        raise KeycloakModelError(
            f"{_PORTABLE_TYPES} or {_ORGANIZATION_BOUND_TYPES} must declare every type client "
            f"in {_AUDIENCES}: {undeclared} is in neither"
        )
    not_audiences = sorted((portable | bound) - type_clients)
    if not_audiences:
        raise KeycloakModelError(
            f"{_PORTABLE_TYPES} and {_ORGANIZATION_BOUND_TYPES} declare types that are not in "
            f"{_AUDIENCES}: {not_audiences}"
        )

    not_portable = sorted(portable_permissions.keys() - portable)
    if not_portable:
        raise KeycloakModelError(
            f"{_PORTABLE_PERMISSIONS} lists permissions for types that are not in {_PORTABLE_TYPES}: {not_portable}"
        )
    without_list = sorted(type_client for type_client in portable if not portable_permissions.get(type_client))
    if without_list:
        raise KeycloakModelError(
            f"{_PORTABLE_PERMISSIONS} must list at least one permission for every portable type: "
            f"none for {without_list}"
        )
    not_read_only = sorted(
        f"{type_client}={permission}"
        for type_client, permissions in portable_permissions.items()
        for permission in permissions
        if not _is_portable(permission)
    )
    if not_read_only:
        raise KeycloakModelError(
            f"{_PORTABLE_PERMISSIONS} may list only <domain>:read and <domain>:list permissions: {not_read_only}"
        )

    return KeycloakTokenModel(
        types=tuple(
            KeycloakClientType(
                type_client=type_client,
                app_client=app_clients[type_client],
                organization_bound=type_client in bound,
                portable_permissions=portable_permissions.get(type_client, frozenset()),
            )
            for type_client in sorted(type_clients)
        )
    )


def local_issuer(issuer_url: str) -> bool:
    """True when the issuer's host is loopback or a ``.localhost`` name.

    The only issuers plain HTTP may be allowed for. ``localhost`` and every
    name under it are reserved for loopback (RFC 6761 §6.3).
    """
    try:
        host = urlsplit(issuer_url).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def single_audience(claims: Mapping[str, object]) -> str | None:
    """The token's one audience: a non-empty string, or a list holding exactly one. Else ``None``."""
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) == 1:
        (aud,) = aud
    return aud if isinstance(aud, str) and aud else None


def binding_failure(model: KeycloakTokenModel, claims: Mapping[str, object]) -> str | None:
    """Why the token is not bound to this API, or ``None`` when it is.

    Bound means exactly one ``aud``, that audience is a configured type client,
    and ``azp`` is the app client configured for it. The reason is for the
    server's log and holds no claim value.
    """
    audience = single_audience(claims)
    if audience is None:
        return "aud is not exactly one value"
    client_type = model.type_for(audience)
    if client_type is None:
        return "aud is not a configured type client"
    azp = claims.get("azp")
    if not isinstance(azp, str) or azp != client_type.app_client:
        return "azp is not the app client configured for the token's aud"
    return None


def organization(claims: Mapping[str, object]) -> str | None:
    """The one organization alias the token names, or ``None``.

    A list must hold exactly one non-empty string, the shape Keycloak sends. A
    map must have exactly one non-empty key, and its value is not read. A
    missing claim and every other type name no organization: Keycloak issues a
    token with no claim to a user outside the organization that was asked for.
    """
    claim = claims.get("organization")
    if isinstance(claim, (list, dict)) and len(claim) == 1:
        (alias,) = claim
        return alias if isinstance(alias, str) and alias else None
    return None


def client_roles(claims: Mapping[str, object], audience: str) -> list[str] | None:
    """``resource_access[audience].roles``, or ``None`` when any part is missing or of the wrong type.

    A list with an entry that is not a string is the wrong type: nothing in it is granted.
    """
    resource_access = claims.get("resource_access")
    entry = resource_access.get(audience) if isinstance(resource_access, dict) else None
    roles = entry.get("roles") if isinstance(entry, dict) else None
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        return None
    return roles


def read_grant(model: KeycloakTokenModel, claims: Mapping[str, object]) -> TokenGrant | None:
    """What the token grants and where, or ``None`` when it names no single organization.

    Also ``None`` for a token that is not bound: the middlewares refuse those
    with 401 before a grant is read, and this does not rely on them. Roles that
    cannot be read grant nothing in the organization the token names. Only
    ``organization`` and ``resource_access[aud]`` are read: never ``realm_access``
    and never another provider's ``dct`` or ``tenants``.
    """
    if binding_failure(model, claims) is not None:
        return None
    alias = organization(claims)
    audience = single_audience(claims)
    client_type = model.type_for(audience) if audience is not None else None
    if alias is None or audience is None or client_type is None:
        return None
    roles = client_roles(claims, audience) or []
    if client_type.organization_bound:
        return TokenGrant(organization=alias, permissions=tuple(roles), organization_bound=True)
    return TokenGrant(
        organization=alias,
        permissions=tuple(role for role in roles if role in client_type.portable_permissions),
        organization_bound=False,
    )
