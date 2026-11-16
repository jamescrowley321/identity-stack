"""Read-only lookup of a Keycloak user's organizations through the Admin REST API.

One call: ``GET /admin/realms/{realm}/organizations/members/{member id}/organizations``
with ``briefRepresentation=false``, so each organization comes with its
attributes. The call is made with a service-account token (client-credentials
grant) of a client that holds only ``realm-management`` ``view-users`` and
``view-organizations``: Keycloak 26.8 opens the endpoint to neither alone, and
both are read-only. The client also needs the ``kc-admin-api`` client scope,
which puts the Admin API audience on its token.

The member id is always the verified token's ``sub``, passed in by the caller;
nothing from the request reaches it. A ``sub`` that is not a well-formed id is
refused before any call (``MemberIdRefused``), and a well-formed one is
URL-encoded into the path.

**Grant markers.** Client roles are realm-wide, so a user's membership does not
show in which organization those roles were granted. An organization records it
in an attribute per user type: ``type:<type client>`` lists the ids of the
members who hold that type there. Only a realm admin can write an organization
attribute; this service account cannot.

A well-formed id the realm does not know (404: the user was deleted while
their token still lives) raises ``MemberUnknown``: such a user belongs to no
organization. A 401 from the Admin API drops the cached service-account token,
and the lookup is asked once more with a fresh one.

Every other failure raises ``KeycloakDirectoryError`` with the HTTP status
(``None`` for no response) and logs that status. The client secret and the
service-account token are never logged and never in a ``repr``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote

from opentelemetry import trace

from app.logbook import logbook
from app.outbound import OutboundClient, UpstreamStatusError, UpstreamTransportError, outbound_client

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# The organization attribute that lists the members holding a user type there.
GRANT_ATTRIBUTE_PREFIX = "type:"
# Each outbound call's own limit (conventions §6). The request waits no longer
# than MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS for the whole lookup either way.
DIRECTORY_TIMEOUT_SECONDS = 4.0
# A cached service-account token is replaced this long before it expires.
_TOKEN_MARGIN_SECONDS = 30.0
# Keycloak's own ids are UUIDs; imported and federated ids may hold ``.``, ``_``
# and ``:``. Nothing that can change the path (``/``, ``%``, ``?``) is an id.
_MEMBER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}")


class MemberIdRefused(ValueError):
    """The token's ``sub`` is not a well-formed member id. No call was made."""


class IssuerNamesNoRealm(ValueError):
    """The issuer is not ``<origin>/realms/<realm>``, so no realm Admin API can be derived from it."""


class MemberUnknown(LookupError):
    """The realm knows no user with this member id (the Admin API answered 404)."""


class KeycloakDirectoryError(Exception):
    """The Admin API could not be asked: no response, a non-2xx status, or an answer of the wrong shape.

    ``status`` is the HTTP status, or ``None`` when there was no response or the
    response could not be read.
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.status = status


class _TokenRefused(Exception):
    """The Admin API answered 401 to the service-account token. Internal: the lookup retries once."""


@dataclass(frozen=True)
class OrganizationMembership:
    """One organization a user belongs to, and the user types its markers grant that user there."""

    alias: str
    name: str
    granted_types: frozenset[str]


def _well_formed_member_id(sub: object) -> bool:
    """Whether ``sub`` may be put in the lookup's path as a member id."""
    return isinstance(sub, str) and _MEMBER_ID.fullmatch(sub) is not None


def _granted_types(attributes: object, member_id: str) -> frozenset[str]:
    """The types whose ``type:<type client>`` attribute lists ``member_id``. Anything malformed grants nothing."""
    if not isinstance(attributes, Mapping):
        return frozenset()
    return frozenset(
        name.removeprefix(GRANT_ATTRIBUTE_PREFIX)
        for name, holders in attributes.items()
        if isinstance(name, str)
        and name.startswith(GRANT_ATTRIBUTE_PREFIX)
        and len(name) > len(GRANT_ATTRIBUTE_PREFIX)
        and isinstance(holders, list)
        and member_id in holders
    )


def _memberships(body: object, member_id: str) -> tuple[OrganizationMembership, ...]:
    """The answer as memberships. An answer that is not a list of organizations with an alias raises."""
    if not isinstance(body, list):
        raise KeycloakDirectoryError("organization lookup answered something that is not a list")
    memberships = []
    for entry in body:
        alias = entry.get("alias") if isinstance(entry, dict) else None
        if not isinstance(alias, str) or not alias:
            raise KeycloakDirectoryError("organization lookup answered an organization with no alias")
        name = entry.get("name")
        memberships.append(
            OrganizationMembership(
                alias=alias,
                name=name if isinstance(name, str) else "",
                granted_types=_granted_types(entry.get("attributes"), member_id),
            )
        )
    return tuple(memberships)


def _admin_api_base(issuer: str) -> str | None:
    """The realm's Admin API base for an issuer ``<origin>/realms/<realm>``; ``None`` for any other form."""
    origin, separator, realm = issuer.rstrip("/").rpartition("/realms/")
    if not separator or not origin or not realm or "/" in realm:
        return None
    return f"{origin}/admin/realms/{realm}"


def _default_client() -> OutboundClient:
    return outbound_client(timeout=DIRECTORY_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class KeycloakDirectory:
    """The organizations of a realm's users, read with one service account.

    ``issuer`` is the realm's issuer, ``<origin>/realms/<realm>``; the token
    endpoint and the Admin API are derived from it, and a directory is not
    built for any other form (``IssuerNamesNoRealm``), so startup refuses it.
    Two directories built from the same settings compare equal.
    """

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    client_factory: Callable[[], OutboundClient] = field(default=_default_client, compare=False, repr=False)
    # The service-account token and when it is to be replaced. Kept for the
    # process; never logged.
    _token: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if _admin_api_base(self.issuer) is None:
            raise IssuerNamesNoRealm("the issuer is not <origin>/realms/<realm>")

    def _admin_realm(self) -> str:
        """The realm's Admin API base (the issuer was checked when the directory was built)."""
        return str(_admin_api_base(self.issuer))

    async def _service_account_token(self, http: OutboundClient) -> str:
        cached = self._token.get("value")
        if isinstance(cached, str) and time.monotonic() < self._token.get("renew_at", 0.0):
            return cached
        try:
            response = await http.post(
                f"{self.issuer.rstrip('/')}/protocol/openid-connect/token",
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
            )
            response.raise_for_status()
            body = response.json()
        except UpstreamStatusError as exc:
            status = exc.response.status_code
            logger.warning("organization lookup: the service-account grant answered %s", status)
            raise KeycloakDirectoryError("service-account grant refused", status=status) from exc
        except UpstreamTransportError as exc:
            logger.warning("organization lookup: no answer from the grant endpoint (%s)", type(exc).__name__)
            raise KeycloakDirectoryError("token endpoint unreachable") from exc
        except ValueError as exc:
            logger.warning("organization lookup: the token endpoint answered something that is not JSON")
            raise KeycloakDirectoryError("token endpoint answer unreadable") from exc
        value = body.get("access_token") if isinstance(body, dict) else None
        lifetime = body.get("expires_in") if isinstance(body, dict) else None
        if not isinstance(value, str) or not value:
            logger.warning("organization lookup: the token endpoint answered with no access token")
            raise KeycloakDirectoryError("token endpoint answer has no access token")
        if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool):
            self._token.update(value=value, renew_at=time.monotonic() + float(lifetime) - _TOKEN_MARGIN_SECONDS)
        return value

    async def organizations_of(self, member_id: str) -> tuple[OrganizationMembership, ...]:
        """Every organization ``member_id`` belongs to, with the types each one's markers grant them.

        Raises ``MemberIdRefused`` before any call for an id that is not well
        formed, ``MemberUnknown`` when the realm has no such user, and
        ``KeycloakDirectoryError`` when the directory cannot be asked. A lookup
        the Admin API answers 401 is asked once more with a fresh
        service-account token; a second 401 is a failure.
        """
        with tracer.start_as_current_span("KeycloakDirectory.organizations_of"):
            if not _well_formed_member_id(member_id):
                logger.warning("organization lookup refused a member id that is not well formed")
                raise MemberIdRefused("the member id is not well formed")
            url = f"{self._admin_realm()}/organizations/members/{quote(member_id, safe='')}/organizations"
            async with self.client_factory() as http:
                try:
                    body = await self._lookup(http, url, member_id)
                except _TokenRefused:
                    # A token the Admin API no longer accepts is not kept: one more try with a new one.
                    self._token.clear()
                    logger.warning("organization lookup answered 401; asking once more with a new token")
                    try:
                        body = await self._lookup(http, url, member_id)
                    except _TokenRefused as exc:
                        self._token.clear()
                        logger.warning("organization lookup answered 401 again (member=%s)", member_id)
                        raise KeycloakDirectoryError("organization lookup refused", status=401) from exc
            return _memberships(body, member_id)

    async def _lookup(self, http: OutboundClient, url: str, member_id: str) -> object:
        """The lookup's JSON answer. Raises ``_TokenRefused`` on a 401, the typed failures otherwise."""
        token = await self._service_account_token(http)
        try:
            response = await http.get(
                url, params={"briefRepresentation": "false"}, headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
            return response.json()
        except UpstreamStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise _TokenRefused from exc
            if status == 404:
                logger.warning("organization lookup answered 404, no such member in the realm (member=%s)", member_id)
                raise MemberUnknown("the realm knows no such member") from exc
            logger.warning("organization lookup answered %s (member=%s)", status, member_id)
            raise KeycloakDirectoryError("organization lookup refused", status=status) from exc
        except UpstreamTransportError as exc:
            logger.warning("organization lookup did not answer (%s)", type(exc).__name__)
            raise KeycloakDirectoryError("organization lookup unreachable") from exc
        except ValueError as exc:
            logger.warning("organization lookup answered something that is not JSON")
            raise KeycloakDirectoryError("organization lookup answer unreadable") from exc
