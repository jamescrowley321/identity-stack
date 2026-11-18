"""Writes to a Keycloak realm through the Admin REST API, for the Keycloak sync adapter.

One service account (client-credentials grant) of a client whose service account
holds the ``realm-management`` roles ``REQUIRED_ROLES`` and the fine-grained
admin permissions ``view``, ``manage`` and ``map-roles-composite`` on the API
client (``KEYCLOAK_AUDIENCE``) alone, and the ``kc-admin-api`` client scope,
which puts the Admin API audience on its token. Each grant was probed on
Keycloak 26.8.2 (``keycloak/fixtures/README.md``, "The admin service account").

The HTTP client is the process's one client for these calls: ``main.lifespan``
builds it with ``KEYCLOAK_ADMIN_TIMEOUT_SECONDS`` and closes it (conventions §4,
§6). Startup checks the credential (``connect_keycloak_admin``), so a missing or
refused one, or one whose service account lacks a required role, stops the
application instead of failing the first write.

Every failure raises ``KeycloakAdminError`` with the HTTP status (``None`` for no
response or an unreadable one). A 404 is read as "not there" only where its body
is the one Keycloak gives for that absent object (``*_ANSWER`` below); Keycloak
answers 404 for realm-wide faults too (organizations disabled, an unknown realm),
and those stay failures. An Admin API 401 drops the cached token and the call is
asked once more with a new one. The client secret and the token are never logged
and never in a ``repr``.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote

from opentelemetry import trace

from app.knobs import knob
from app.logbook import logbook
from app.outbound import OutboundClient, OutboundResponse, UpstreamStatusError, UpstreamTransportError

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

ISSUER_VARIABLE = "KEYCLOAK_ISSUER_URL"
ADMIN_CLIENT_ID_VARIABLE = "KEYCLOAK_ADMIN_CLIENT_ID"
ADMIN_CREDENTIAL_VARIABLE = "KEYCLOAK_ADMIN_CLIENT_SECRET"
# The API client: its client roles are the canonical permissions and roles.
API_CLIENT_VARIABLE = "KEYCLOAK_AUDIENCE"

# Each call's own limit (conventions §6); main.lifespan builds the client with it.
ADMIN_TIMEOUT_SECONDS = 5.0
# A cached service-account token is replaced this long before it expires.
_TOKEN_MARGIN_SECONDS = 30.0

# The bodies Keycloak 26.8.2 answers for an object that is not there, recorded in
# tests/fixtures/keycloak_admin/responses.json beside the realm-wide 404s, which
# carry other bodies.
ORGANIZATION_NOT_FOUND_ANSWER = {"errorMessage": "Organization not found."}
USER_NOT_FOUND_ANSWER = {"error": "User not found"}
ALREADY_A_MEMBER_ANSWER = {"errorMessage": "User is already a member of the organization."}
ROLE_NOT_FOUND_ANSWER = {"error": "Could not find role with id"}
GROUP_NOT_FOUND_ANSWER = {"errorMessage": "Group does not exist"}
ALREADY_IN_GROUP_ANSWER = {"errorMessage": "User is already a member of the group"}
NOT_IN_GROUP_ANSWER = {"errorMessage": "User not a member"}
# Keycloak's generic 404, answered for a missing membership and for any route
# that does not match: never read as "not there" on its own (#619).
GENERIC_NOT_FOUND_ANSWER = {"error": "HTTP 404 Not Found"}

# The client holding the realm's admin roles, and the ones the adapter needs:
# users and organizations (with their groups) to write, the API client to find,
# and the events the pull reads.
REALM_MANAGEMENT_CLIENT = "realm-management"
REQUIRED_ROLES = frozenset({"manage-users", "manage-organizations", "query-clients", "view-events"})
# What the API client's ``access`` must say for the service account: it reads the
# client and manages its roles (the fine-grained ``view`` and ``manage``).
REQUIRED_API_CLIENT_ACCESS = frozenset({"view", "manage"})
# Page size of the lists the adapter reads whole (organizations, groups).
PAGE_SIZE = 100
# Keycloak names a client's service account this, in lower case like every username.
_SERVICE_ACCOUNT_PREFIX = "service-account-"


class EventStream(enum.StrEnum):
    """The realm's two event stores, by their Admin API path."""

    ADMIN = "/admin-events"
    USER = "/events"


class KeycloakAdminNotConfigured(ValueError):
    """A setting the admin client needs is missing or names no realm. No call was made."""


class KeycloakAdminError(Exception):
    """The Admin API could not do what was asked: no response, a refusal, or an answer of the wrong shape.

    ``status`` is the HTTP status, or ``None`` when there was no response or it could not be read.
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.status = status


class KeycloakAdminUnavailable(KeycloakAdminError):
    """Keycloak could not be asked: no answer, or no service-account token. Asking again later may work."""


class KeycloakAdminRolesMissing(KeycloakAdminError):
    """The service account Keycloak accepted does not hold every ``realm-management`` role the adapter writes with.

    ``missing`` names the roles it lacks. Raised at startup, so a read-only
    credential stops the application instead of failing the first write.
    """

    def __init__(self, missing: frozenset[str]) -> None:
        super().__init__(f"service account lacks realm-management roles {sorted(missing)}", status=403)
        self.missing = missing


class KeycloakApiClientNotManaged(KeycloakAdminError):
    """The service account cannot find the API client, or may not read it and manage its roles.

    Raised at startup, like ``KeycloakAdminRolesMissing``: the adapter keeps the
    canonical permissions and roles as that client's roles. ``missing`` names the
    ``access`` entries Keycloak did not grant (all of them when the client was not found).
    """

    def __init__(self, client_id: str, missing: frozenset[str], *, found: bool) -> None:
        what = f"lacks {sorted(missing)} on" if found else "cannot find"
        super().__init__(f"service account {what} the API client {client_id!r}", status=403)
        self.missing = missing
        self.found = found


class _TokenRefused(Exception):
    """The Admin API answered 401 to the service-account token. Internal: the call is asked once more."""


@dataclass(frozen=True)
class KeycloakAdminSettings:
    """The realm (by its issuer, ``<origin>/realms/<realm>``) and the admin client's credential."""

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    api_client_id: str

    def _admin_realm(self) -> str:
        origin, _, realm = self.issuer.rstrip("/").rpartition("/realms/")
        return f"{origin}/admin/realms/{realm}"

    def _token_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/protocol/openid-connect/token"


def _names_a_realm(issuer: str) -> bool:
    origin, separator, realm = issuer.rstrip("/").rpartition("/realms/")
    return bool(separator and origin and realm and "/" not in realm)


def keycloak_admin_settings() -> KeycloakAdminSettings:
    """Read the admin client's settings. Raises ``KeycloakAdminNotConfigured`` naming the first one missing."""
    with tracer.start_as_current_span("keycloak_admin.keycloak_admin_settings"):
        values = {
            ISSUER_VARIABLE: knob("KEYCLOAK_ISSUER_URL") or "",
            ADMIN_CLIENT_ID_VARIABLE: knob("KEYCLOAK_ADMIN_CLIENT_ID") or "",
            ADMIN_CREDENTIAL_VARIABLE: knob("KEYCLOAK_ADMIN_CLIENT_SECRET") or "",
            API_CLIENT_VARIABLE: (knob("KEYCLOAK_AUDIENCE") or "").strip(),
        }
        for name, value in values.items():
            if not value:
                raise KeycloakAdminNotConfigured(f"{name} is not set")
        if not _names_a_realm(values[ISSUER_VARIABLE]):
            raise KeycloakAdminNotConfigured(f"{ISSUER_VARIABLE} is not <origin>/realms/<realm>")
        return KeycloakAdminSettings(
            issuer=values[ISSUER_VARIABLE],
            client_id=values[ADMIN_CLIENT_ID_VARIABLE],
            client_secret=values[ADMIN_CREDENTIAL_VARIABLE],
            api_client_id=values[API_CLIENT_VARIABLE],
        )


async def connect_keycloak_admin(http: OutboundClient) -> KeycloakAdminApi:
    """The admin client on ``http``, once its credential has been checked against the realm.

    Raises ``KeycloakAdminNotConfigured`` before any call for a missing setting,
    ``KeycloakAdminRolesMissing`` when the service account lacks a required role,
    ``KeycloakApiClientNotManaged`` when it cannot manage the API client's roles,
    and ``KeycloakAdminError`` when Keycloak refuses the credential or cannot be asked.
    """
    with tracer.start_as_current_span("keycloak_admin.connect_keycloak_admin"):
        api = KeycloakAdminApi(keycloak_admin_settings(), http=http)
        await api.check_credentials()
        return api


def _body(response: OutboundResponse) -> object:
    try:
        return response.json()
    except ValueError:
        return None


class KeycloakAdminApi:
    """The Admin API calls the Keycloak sync adapter makes, on one HTTP client and one service account."""

    def __init__(self, settings: KeycloakAdminSettings, *, http: OutboundClient) -> None:
        self.settings = settings
        self._http = http
        # The service-account token and when it is to be replaced. Never logged.
        self._token: dict = {}
        # The API client's Keycloak id, read once.
        self._api_client: str | None = None
        # The Keycloak id of the admin client's service account, read once.
        self._service_account: str | None = None

    def __repr__(self) -> str:
        return f"KeycloakAdminApi(issuer={self.settings.issuer!r}, client_id={self.settings.client_id!r})"

    async def _service_account_token(self) -> str:
        cached = self._token.get("value")
        if isinstance(cached, str) and time.monotonic() < self._token.get("renew_at", 0.0):
            return cached
        try:
            response = await self._http.post(
                self.settings._token_url(),
                data={"grant_type": "client_credentials"},
                auth=(self.settings.client_id, self.settings.client_secret),
            )
            response.raise_for_status()
            body = response.json()
        except UpstreamStatusError as exc:
            status = exc.response.status_code
            logger.warning("keycloak admin: the service-account grant answered %s", status)
            raise KeycloakAdminUnavailable("service-account grant refused", status=status) from exc
        except UpstreamTransportError as exc:
            logger.warning("keycloak admin: no answer from the grant endpoint (%s)", type(exc).__name__)
            raise KeycloakAdminUnavailable("token endpoint unreachable") from exc
        except ValueError as exc:
            logger.warning("keycloak admin: the grant endpoint answered something that is not JSON")
            raise KeycloakAdminUnavailable("token endpoint answer unreadable") from exc
        value = body.get("access_token") if isinstance(body, dict) else None
        lifetime = body.get("expires_in") if isinstance(body, dict) else None
        if not isinstance(value, str) or not value:
            logger.warning("keycloak admin: the grant endpoint answered with no access token")
            raise KeycloakAdminUnavailable("token endpoint answer has no access token")
        if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool):
            self._token.update(value=value, renew_at=time.monotonic() + float(lifetime) - _TOKEN_MARGIN_SECONDS)
        return value

    async def _call(
        self, method: str, path: str, *, params: dict | None = None, json: object = None
    ) -> OutboundResponse:
        """One Admin API call, asked once more with a new token after a 401. Non-2xx answers are returned."""

        async def once() -> OutboundResponse:
            token = await self._service_account_token()
            try:
                response = await self._http.request(
                    method,
                    f"{self.settings._admin_realm()}{path}",
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except UpstreamTransportError as exc:
                logger.warning("keycloak admin: %s %s did not answer (%s)", method, path, type(exc).__name__)
                raise KeycloakAdminUnavailable("admin API unreachable") from exc
            if response.status_code == 401:
                raise _TokenRefused
            return response

        try:
            return await once()
        except _TokenRefused:
            self._token.clear()
            logger.warning("keycloak admin: %s %s answered 401; asking once more with a new token", method, path)
        try:
            return await once()
        except _TokenRefused as exc:
            self._token.clear()
            logger.warning("keycloak admin: %s %s answered 401 again", method, path)
            raise KeycloakAdminError("admin API refused the service-account token", status=401) from exc

    @staticmethod
    def _refused(method: str, path: str, response: OutboundResponse) -> KeycloakAdminError:
        status = response.status_code
        logger.warning("keycloak admin: %s %s answered %s: %s", method, path, status, response.text[:500])
        return KeycloakAdminError("admin API refused the call", status=status)

    async def _expect(
        self,
        method: str,
        path: str,
        *,
        ok: frozenset[int],
        params: dict | None = None,
        json: object = None,
        also_done: Callable[[OutboundResponse], bool] = lambda _response: False,
    ) -> OutboundResponse:
        response = await self._call(method, path, params=params, json=json)
        if response.status_code in ok or also_done(response):
            return response
        raise self._refused(method, path, response)

    async def _find_one(self, path: str, params: dict, key: str, value: str) -> dict | None:
        response = await self._expect("GET", path, ok=frozenset({200}), params=params)
        found = _body(response)
        if not isinstance(found, list) or not all(isinstance(entry, dict) for entry in found):
            logger.warning("keycloak admin: GET %s answered something that is not a list of objects", path)
            raise KeycloakAdminError("admin API answer unreadable")
        matches = [entry for entry in found if entry.get(key) == value]
        if len(matches) > 1:
            logger.warning("keycloak admin: GET %s answered %s matches", path, len(matches))
            raise KeycloakAdminError("admin API answered several matches")
        if matches and not isinstance(matches[0].get("id"), str):
            raise KeycloakAdminError("admin API answered an object with no id")
        return matches[0] if matches else None

    async def check_credentials(self) -> None:
        """Take a token, read the Admin API with it, and confirm the service account holds ``REQUIRED_ROLES``.

        The token carries no roles (the client has only the ``kc-admin-api``
        scope), so the roles are read from the service account's own role
        mappings: its direct ones name the ``realm-management`` client, and that
        client's effective (composite) roles must include every required one.
        Raises ``KeycloakAdminRolesMissing`` when they do not. Then the API client
        must be found and its ``access`` must grant ``REQUIRED_API_CLIENT_ACCESS``
        (the fine-grained permissions, as Keycloak reports them for the caller):
        ``KeycloakApiClientNotManaged`` otherwise. ``KeycloakAdminError`` on any
        other failure.
        """
        with tracer.start_as_current_span("KeycloakAdminApi.check_credentials"):
            await self._expect("GET", "/organizations", ok=frozenset({200}), params={"first": "0", "max": "1"})
            held = await self._realm_management_roles()
            missing = REQUIRED_ROLES - held
            if missing:
                logger.warning("keycloak admin: the service account lacks realm-management roles %s", sorted(missing))
                raise KeycloakAdminRolesMissing(frozenset(missing))
            client = await self._find_api_client()
            access = client.get("access") if client is not None else None
            granted = frozenset(
                name for name, value in (access.items() if isinstance(access, dict) else ()) if value is True
            )
            lacking = REQUIRED_API_CLIENT_ACCESS - granted
            if client is None or lacking:
                logger.warning(
                    "keycloak admin: the service account cannot manage the API client (found: %s, lacking %s)",
                    client is not None,
                    sorted(lacking),
                )
                raise KeycloakApiClientNotManaged(
                    self.settings.api_client_id, frozenset(lacking), found=client is not None
                )
            self._api_client = client["id"]

    async def _realm_management_roles(self) -> frozenset[str]:
        """The ``realm-management`` roles the service account holds, composites expanded. Unreadable: none."""
        account = await self.find_user(f"{_SERVICE_ACCOUNT_PREFIX}{self.settings.client_id}".lower())
        if account is None:
            return frozenset()
        self._service_account = account["id"]
        mappings_path = f"/users/{quote(account['id'], safe='')}/role-mappings"
        mappings = _body(await self._expect("GET", mappings_path, ok=frozenset({200})))
        client_mappings = mappings.get("clientMappings") if isinstance(mappings, dict) else None
        client = client_mappings.get(REALM_MANAGEMENT_CLIENT) if isinstance(client_mappings, dict) else None
        client_id = client.get("id") if isinstance(client, dict) else None
        if not isinstance(client_id, str):
            return frozenset()
        roles_path = f"{mappings_path}/clients/{quote(client_id, safe='')}/composite"
        roles = _body(await self._expect("GET", roles_path, ok=frozenset({200})))
        if not isinstance(roles, list):
            raise KeycloakAdminError("admin API answer unreadable")
        return frozenset(role["name"] for role in roles if isinstance(role, dict) and isinstance(role.get("name"), str))

    async def find_organization(self, alias: str) -> dict | None:
        """The organization with exactly this alias, or ``None``."""
        with tracer.start_as_current_span("KeycloakAdminApi.find_organization"):
            return await self._find_one("/organizations", {"q": f"alias:{alias}", "exact": "true"}, "alias", alias)

    async def create_organization(self, representation: dict) -> None:
        with tracer.start_as_current_span("KeycloakAdminApi.create_organization"):
            await self._expect("POST", "/organizations", ok=frozenset({201}), json=representation)

    async def update_organization(self, organization_id: str, representation: dict) -> None:
        """Replace the organization. Keycloak keeps the attributes and domains a representation leaves out
        and resets ``enabled``, ``description`` and ``redirectUrl``: send those as found."""
        with tracer.start_as_current_span("KeycloakAdminApi.update_organization"):
            await self._expect(
                "PUT", f"/organizations/{quote(organization_id, safe='')}", ok=frozenset({204}), json=representation
            )

    async def delete_organization(self, organization_id: str) -> None:
        """Delete it; an organization already gone is deleted."""
        with tracer.start_as_current_span("KeycloakAdminApi.delete_organization"):
            await self._expect(
                "DELETE",
                f"/organizations/{quote(organization_id, safe='')}",
                ok=frozenset({204}),
                also_done=_answers(404, ORGANIZATION_NOT_FOUND_ANSWER),
            )

    async def find_user(self, username: str) -> dict | None:
        """The user with exactly this username, or ``None``."""
        with tracer.start_as_current_span("KeycloakAdminApi.find_user"):
            return await self._find_one("/users", {"username": username, "exact": "true"}, "username", username)

    async def get_user(self, user_id: str) -> dict | None:
        """The user with this Keycloak id, or ``None`` when the realm has none."""
        with tracer.start_as_current_span("KeycloakAdminApi.get_user"):
            response = await self._expect(
                "GET",
                f"/users/{quote(user_id, safe='')}",
                ok=frozenset({200}),
                also_done=_answers(404, USER_NOT_FOUND_ANSWER),
            )
            if response.status_code == 404:
                return None
            found = _body(response)
            if not isinstance(found, dict) or found.get("id") != user_id:
                raise KeycloakAdminError("admin API answer unreadable")
            return found

    async def create_user(self, representation: dict) -> None:
        with tracer.start_as_current_span("KeycloakAdminApi.create_user"):
            await self._expect("POST", "/users", ok=frozenset({201}), json=representation)

    async def update_user(self, user_id: str, representation: dict) -> None:
        with tracer.start_as_current_span("KeycloakAdminApi.update_user"):
            await self._expect("PUT", f"/users/{quote(user_id, safe='')}", ok=frozenset({204}), json=representation)

    async def delete_user(self, user_id: str) -> None:
        """Delete them; a user already gone is deleted."""
        with tracer.start_as_current_span("KeycloakAdminApi.delete_user"):
            await self._expect(
                "DELETE",
                f"/users/{quote(user_id, safe='')}",
                ok=frozenset({204}),
                also_done=_answers(404, USER_NOT_FOUND_ANSWER),
            )

    async def add_member(self, organization_id: str, user_id: str) -> None:
        """Make the user a member; one already a member is added."""
        with tracer.start_as_current_span("KeycloakAdminApi.add_member"):
            await self._expect(
                "POST",
                f"/organizations/{quote(organization_id, safe='')}/members",
                ok=frozenset({201}),
                json=user_id,
                also_done=_answers(409, ALREADY_A_MEMBER_ANSWER),
            )

    async def remove_member(self, organization_id: str, user_id: str) -> None:
        """End the membership; a user who is not a member, or an organization gone, is removed.

        Keycloak answers a missing membership with its generic 404, the answer any
        route that does not match gets too, so that 404 counts as done only once
        the user's organizations are read and this one is not among them (#619).
        """
        with tracer.start_as_current_span("KeycloakAdminApi.remove_member"):
            path = f"/organizations/{quote(organization_id, safe='')}/members/{quote(user_id, safe='')}"
            response = await self._expect(
                "DELETE",
                path,
                ok=frozenset({204}),
                also_done=_answers(404, GENERIC_NOT_FOUND_ANSWER, ORGANIZATION_NOT_FOUND_ANSWER),
            )
            if _body(response) == GENERIC_NOT_FOUND_ANSWER and organization_id in await self.organizations_of(user_id):
                logger.warning("keycloak admin: DELETE %s answered 404 for a member it still has", path)
                raise KeycloakAdminError("admin API answered 404 for a membership that is there", status=404)

    async def organizations_of(self, user_id: str) -> frozenset[str]:
        """The ids of the organizations the user is a member of. A user the realm lacks is a member of none."""
        with tracer.start_as_current_span("KeycloakAdminApi.organizations_of"):
            path = f"/organizations/members/{quote(user_id, safe='')}/organizations"
            response = await self._expect(
                "GET", path, ok=frozenset({200}), also_done=_answers(404, GENERIC_NOT_FOUND_ANSWER)
            )
            if response.status_code == 404:
                # The generic 404 again: only a user the realm does not have makes it "none".
                if await self.get_user(user_id) is not None:
                    raise self._refused("GET", path, response)
                return frozenset()
            return frozenset(entry["id"] for entry in _objects(response, path))

    async def list_organizations(self) -> list[dict]:
        """Every organization of the realm, read a page at a time."""
        with tracer.start_as_current_span("KeycloakAdminApi.list_organizations"):
            return await self._all("/organizations", {"briefRepresentation": "true"})

    async def organizations(self) -> list[dict]:
        """Every organization of the realm with its domains and ``enabled``, read a page at a time."""
        with tracer.start_as_current_span("KeycloakAdminApi.organizations"):
            return await self._all("/organizations", {"briefRepresentation": "false"})

    async def organization_members(self, organization_id: str) -> list[dict]:
        """Every member of the organization, read a page at a time."""
        with tracer.start_as_current_span("KeycloakAdminApi.organization_members"):
            return await self._all(f"/organizations/{quote(organization_id, safe='')}/members", {})

    async def organization_group_members(self, organization_id: str, group_id: str) -> list[dict]:
        """Every member of one of the organization's groups, read a page at a time."""
        with tracer.start_as_current_span("KeycloakAdminApi.organization_group_members"):
            path = f"/organizations/{quote(organization_id, safe='')}/groups/{quote(group_id, safe='')}/members"
            return await self._all(path, {})

    async def users(self) -> list[dict]:
        """Every user of the realm, read a page at a time."""
        with tracer.start_as_current_span("KeycloakAdminApi.users"):
            return await self._all("/users", {"briefRepresentation": "false"})

    async def organization_groups(self, organization_id: str) -> list[dict]:
        """Every group the organization owns, attributes included."""
        with tracer.start_as_current_span("KeycloakAdminApi.organization_groups"):
            path = f"/organizations/{quote(organization_id, safe='')}/groups"
            return await self._all(path, {"briefRepresentation": "false"})

    async def create_organization_group(self, organization_id: str, representation: dict) -> None:
        with tracer.start_as_current_span("KeycloakAdminApi.create_organization_group"):
            path = f"/organizations/{quote(organization_id, safe='')}/groups"
            await self._expect("POST", path, ok=frozenset({201}), json=representation)

    async def update_organization_group(self, organization_id: str, group_id: str, representation: dict) -> None:
        with tracer.start_as_current_span("KeycloakAdminApi.update_organization_group"):
            path = f"/organizations/{quote(organization_id, safe='')}/groups/{quote(group_id, safe='')}"
            await self._expect("PUT", path, ok=frozenset({204}), json=representation)

    async def delete_organization_group(self, organization_id: str, group_id: str) -> None:
        """Delete it; a group already gone is deleted."""
        with tracer.start_as_current_span("KeycloakAdminApi.delete_organization_group"):
            path = f"/organizations/{quote(organization_id, safe='')}/groups/{quote(group_id, safe='')}"
            await self._expect("DELETE", path, ok=frozenset({204}), also_done=_answers(404, GROUP_NOT_FOUND_ANSWER))

    async def add_group_member(self, organization_id: str, group_id: str, user_id: str) -> None:
        """Put an organization member in the organization's group; one already in it is added."""
        with tracer.start_as_current_span("KeycloakAdminApi.add_group_member"):
            path = (
                f"/organizations/{quote(organization_id, safe='')}/groups/{quote(group_id, safe='')}"
                f"/members/{quote(user_id, safe='')}"
            )
            await self._expect("PUT", path, ok=frozenset({204}), also_done=_answers(409, ALREADY_IN_GROUP_ANSWER))

    async def remove_group_member(self, organization_id: str, group_id: str, user_id: str) -> None:
        """Take the user out of the organization's group; one not in it, or a group gone, is removed."""
        with tracer.start_as_current_span("KeycloakAdminApi.remove_group_member"):
            path = (
                f"/organizations/{quote(organization_id, safe='')}/groups/{quote(group_id, safe='')}"
                f"/members/{quote(user_id, safe='')}"
            )
            await self._expect(
                "DELETE",
                path,
                ok=frozenset({204}),
                also_done=lambda response: (
                    _answers(400, NOT_IN_GROUP_ANSWER)(response) or _answers(404, GROUP_NOT_FOUND_ANSWER)(response)
                ),
            )

    async def _find_api_client(self) -> dict | None:
        """The API client (``api_client_id``), with the ``access`` Keycloak grants the caller on it."""
        client_id = self.settings.api_client_id
        return await self._find_one("/clients", {"clientId": client_id}, "clientId", client_id)

    async def _api_client_uuid(self) -> str:
        if self._api_client is None:
            client = await self._find_api_client()
            if client is None:
                raise KeycloakApiClientNotManaged(self.settings.api_client_id, REQUIRED_API_CLIENT_ACCESS, found=False)
            self._api_client = client["id"]
        return self._api_client

    async def _api_client_path(self) -> str:
        return f"/clients/{quote(await self._api_client_uuid(), safe='')}"

    async def client_roles(self) -> list[dict]:
        """Every role of the API client, attributes included."""
        with tracer.start_as_current_span("KeycloakAdminApi.client_roles"):
            path = f"{await self._api_client_path()}/roles"
            response = await self._expect("GET", path, ok=frozenset({200}), params={"briefRepresentation": "false"})
            return _objects(response, path)

    async def create_client_role(self, representation: dict) -> None:
        with tracer.start_as_current_span("KeycloakAdminApi.create_client_role"):
            await self._expect(
                "POST", f"{await self._api_client_path()}/roles", ok=frozenset({201}), json=representation
            )

    async def update_role(self, role_id: str, representation: dict) -> None:
        """Replace the role (by its Keycloak id): name, description and attributes."""
        with tracer.start_as_current_span("KeycloakAdminApi.update_role"):
            await self._expect(
                "PUT", f"/roles-by-id/{quote(role_id, safe='')}", ok=frozenset({204}), json=representation
            )

    async def delete_role(self, role_id: str) -> None:
        """Delete it; a role already gone is deleted. Keycloak drops it from every composite."""
        with tracer.start_as_current_span("KeycloakAdminApi.delete_role"):
            await self._expect(
                "DELETE",
                f"/roles-by-id/{quote(role_id, safe='')}",
                ok=frozenset({204}),
                also_done=_answers(404, ROLE_NOT_FOUND_ANSWER),
            )

    async def role_composites(self, role_id: str) -> list[dict]:
        with tracer.start_as_current_span("KeycloakAdminApi.role_composites"):
            path = f"/roles-by-id/{quote(role_id, safe='')}/composites"
            return _objects(await self._expect("GET", path, ok=frozenset({200})), path)

    async def add_composites(self, role_id: str, composite_ids: list[str]) -> None:
        with tracer.start_as_current_span("KeycloakAdminApi.add_composites"):
            path = f"/roles-by-id/{quote(role_id, safe='')}/composites"
            await self._expect("POST", path, ok=frozenset({204}), json=[{"id": entry} for entry in composite_ids])

    async def remove_composites(self, role_id: str, composite_ids: list[str]) -> None:
        with tracer.start_as_current_span("KeycloakAdminApi.remove_composites"):
            path = f"/roles-by-id/{quote(role_id, safe='')}/composites"
            await self._expect("DELETE", path, ok=frozenset({204}), json=[{"id": entry} for entry in composite_ids])

    async def service_account_id(self) -> str:
        """The Keycloak id of the admin client's service account: the user its own admin events name."""
        with tracer.start_as_current_span("KeycloakAdminApi.service_account_id"):
            if self._service_account is None:
                account = await self.find_user(f"{_SERVICE_ACCOUNT_PREFIX}{self.settings.client_id}".lower())
                if account is None:
                    raise KeycloakAdminError("the admin client's service account is not in the realm")
                self._service_account = account["id"]
            return self._service_account

    async def api_client_uuid(self) -> str:
        """The API client's Keycloak id, which the paths of its client roles' admin events carry."""
        with tracer.start_as_current_span("KeycloakAdminApi.api_client_uuid"):
            return await self._api_client_uuid()

    async def get_organization(self, organization_id: str) -> dict | None:
        """The organization with this Keycloak id, or ``None`` when the realm has none."""
        with tracer.start_as_current_span("KeycloakAdminApi.get_organization"):
            response = await self._expect(
                "GET",
                f"/organizations/{quote(organization_id, safe='')}",
                ok=frozenset({200}),
                also_done=_answers(404, ORGANIZATION_NOT_FOUND_ANSWER),
            )
            if response.status_code == 404:
                return None
            found = _body(response)
            if not isinstance(found, dict) or found.get("id") != organization_id:
                raise KeycloakAdminError("admin API answer unreadable")
            return found

    async def events(
        self, stream: EventStream, *, date_from: int, first: int, max_results: int, newest: bool = False
    ) -> list[dict]:
        """One page of a stream's events recorded at or after ``date_from`` (milliseconds), oldest first.

        With ``newest`` the page is newest first instead. Each event must have a
        string ``id`` and an integer ``time``; any other answer is unreadable.
        """
        with tracer.start_as_current_span("KeycloakAdminApi.events") as span:
            span.set_attribute("keycloak.event_stream", stream.name)
            params = {
                "dateFrom": str(date_from),
                "first": str(first),
                "max": str(max_results),
                "direction": "desc" if newest else "asc",
            }
            response = await self._expect("GET", stream.value, ok=frozenset({200}), params=params)
            page = _objects(response, stream.value)
            if not all(isinstance(event.get("time"), int) and not isinstance(event["time"], bool) for event in page):
                logger.warning("keycloak admin: GET %s answered an event without a time", stream.value)
                raise KeycloakAdminError("admin API answer unreadable")
            return page

    async def _all(self, path: str, params: dict) -> list[dict]:
        """Every object of a paged list, ``PAGE_SIZE`` at a time."""
        found: list[dict] = []
        seen: set[str] = set()
        while True:
            page_params = {**params, "first": str(len(found)), "max": str(PAGE_SIZE)}
            response = await self._expect("GET", path, ok=frozenset({200}), params=page_params)
            page = _objects(response, path)
            if page and all(entry["id"] in seen for entry in page):
                # Keycloak answered the same page again: it ignores the paging, and the loop would not end.
                logger.warning("keycloak admin: GET %s repeated a page", path)
                raise KeycloakAdminError("admin API ignores paging", status=response.status_code)
            seen.update(entry["id"] for entry in page)
            found.extend(page)
            if len(page) < PAGE_SIZE:
                return found


def _objects(response: OutboundResponse, path: str) -> list[dict]:
    """The answer as a list of objects that each have an id; anything else is unreadable."""
    found = _body(response)
    if not isinstance(found, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("id"), str) for entry in found
    ):
        logger.warning("keycloak admin: GET %s answered something that is not a list of objects with ids", path)
        raise KeycloakAdminError("admin API answer unreadable")
    return found


def _answers(status: int, *bodies: dict) -> Callable[[OutboundResponse], bool]:
    """Whether a response is ``status`` with one of ``bodies``: Keycloak's answer for an object not there."""

    def matches(response: OutboundResponse) -> bool:
        return response.status_code == status and _body(response) in bodies

    return matches
