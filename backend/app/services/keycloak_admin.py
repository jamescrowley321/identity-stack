"""Writes to a Keycloak realm through the Admin REST API, for the Keycloak sync adapter.

One service account (client-credentials grant) of a client whose service account
holds ``realm-management`` ``manage-users`` and ``manage-organizations``, and the
``kc-admin-api`` client scope, which puts the Admin API audience on its token.
Probed on Keycloak 26.8.2, those two roles cover every call below and nothing
else is needed.

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

# Each call's own limit (conventions §6); main.lifespan builds the client with it.
ADMIN_TIMEOUT_SECONDS = 5.0
# A cached service-account token is replaced this long before it expires.
_TOKEN_MARGIN_SECONDS = 30.0

# The bodies Keycloak 26.8.2 answers for an object that is not there, recorded in
# tests/fixtures/keycloak_admin/responses.json beside the realm-wide 404s, which
# carry other bodies.
ORGANIZATION_NOT_FOUND_ANSWER = {"errorMessage": "Organization not found."}
USER_NOT_FOUND_ANSWER = {"error": "User not found"}
NOT_A_MEMBER_ANSWER = {"error": "HTTP 404 Not Found"}
ALREADY_A_MEMBER_ANSWER = {"errorMessage": "User is already a member of the organization."}

# The client holding the realm's admin roles, and the ones every write below needs.
REALM_MANAGEMENT_CLIENT = "realm-management"
REQUIRED_ROLES = frozenset({"manage-users", "manage-organizations"})
# Keycloak names a client's service account this, in lower case like every username.
_SERVICE_ACCOUNT_PREFIX = "service-account-"


class KeycloakAdminNotConfigured(ValueError):
    """A setting the admin client needs is missing or names no realm. No call was made."""


class KeycloakAdminError(Exception):
    """The Admin API could not do what was asked: no response, a refusal, or an answer of the wrong shape.

    ``status`` is the HTTP status, or ``None`` when there was no response or it could not be read.
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.status = status


class KeycloakAdminRolesMissing(KeycloakAdminError):
    """The service account Keycloak accepted does not hold every ``realm-management`` role the adapter writes with.

    ``missing`` names the roles it lacks. Raised at startup, so a read-only
    credential stops the application instead of failing the first write.
    """

    def __init__(self, missing: frozenset[str]) -> None:
        super().__init__(f"service account lacks realm-management roles {sorted(missing)}", status=403)
        self.missing = missing


class _TokenRefused(Exception):
    """The Admin API answered 401 to the service-account token. Internal: the call is asked once more."""


@dataclass(frozen=True)
class KeycloakAdminSettings:
    """The realm (by its issuer, ``<origin>/realms/<realm>``) and the admin client's credential."""

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)

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
        )


async def connect_keycloak_admin(http: OutboundClient) -> KeycloakAdminApi:
    """The admin client on ``http``, once its credential has been checked against the realm.

    Raises ``KeycloakAdminNotConfigured`` before any call for a missing setting,
    ``KeycloakAdminRolesMissing`` when the service account lacks a required role,
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
            raise KeycloakAdminError("service-account grant refused", status=status) from exc
        except UpstreamTransportError as exc:
            logger.warning("keycloak admin: no answer from the grant endpoint (%s)", type(exc).__name__)
            raise KeycloakAdminError("token endpoint unreachable") from exc
        except ValueError as exc:
            logger.warning("keycloak admin: the grant endpoint answered something that is not JSON")
            raise KeycloakAdminError("token endpoint answer unreadable") from exc
        value = body.get("access_token") if isinstance(body, dict) else None
        lifetime = body.get("expires_in") if isinstance(body, dict) else None
        if not isinstance(value, str) or not value:
            logger.warning("keycloak admin: the grant endpoint answered with no access token")
            raise KeycloakAdminError("token endpoint answer has no access token")
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
                raise KeycloakAdminError("admin API unreachable") from exc
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
        Raises ``KeycloakAdminRolesMissing`` when they do not, ``KeycloakAdminError``
        on any other failure.
        """
        with tracer.start_as_current_span("KeycloakAdminApi.check_credentials"):
            await self._expect("GET", "/organizations", ok=frozenset({200}), params={"first": "0", "max": "1"})
            held = await self._realm_management_roles()
            missing = REQUIRED_ROLES - held
            if missing:
                logger.warning("keycloak admin: the service account lacks realm-management roles %s", sorted(missing))
                raise KeycloakAdminRolesMissing(frozenset(missing))

    async def _realm_management_roles(self) -> frozenset[str]:
        """The ``realm-management`` roles the service account holds, composites expanded. Unreadable: none."""
        account = await self.find_user(f"{_SERVICE_ACCOUNT_PREFIX}{self.settings.client_id}".lower())
        if account is None:
            return frozenset()
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
        """End the membership; a user who is not a member, or an organization gone, is removed."""
        with tracer.start_as_current_span("KeycloakAdminApi.remove_member"):
            await self._expect(
                "DELETE",
                f"/organizations/{quote(organization_id, safe='')}/members/{quote(user_id, safe='')}",
                ok=frozenset({204}),
                also_done=_answers(404, NOT_A_MEMBER_ANSWER, ORGANIZATION_NOT_FOUND_ANSWER),
            )


def _answers(status: int, *bodies: dict) -> Callable[[OutboundResponse], bool]:
    """Whether a response is ``status`` with one of ``bodies``: Keycloak's answer for an object not there."""

    def matches(response: OutboundResponse) -> bool:
        return response.status_code == status and _body(response) in bodies

    return matches
