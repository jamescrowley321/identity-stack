"""A stand-in for the Keycloak Admin API the organization lookup reads: canned answers, every request recorded."""

import httpx

from app.services.keycloak_directory import DIRECTORY_TIMEOUT_SECONDS, GRANT_ATTRIBUTE_PREFIX, KeycloakDirectory
from tests.helpers.keycloak_model import DIRECTORY_CLIENT_ID, DIRECTORY_CLIENT_SECRET, TENANT_ADMIN_TYPE_CLIENT

ORIGIN = "http://keycloak.localhost:8180"
ISSUER = f"{ORIGIN}/realms/identity-stack"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
ADMIN_REALM = f"{ORIGIN}/admin/realms/identity-stack"
SERVICE_ACCOUNT_TOKEN = "service-account-token-value"
MEMBER_ID = "f4a4c000-0000-4000-8000-000000000006"
OTHER_MEMBER_ID = "b0b00000-0000-4000-8000-000000000002"
ACME = "acme"
GLOBEX = "globex"
TENANT_ADMIN_MARKER = f"{GRANT_ATTRIBUTE_PREFIX}{TENANT_ADMIN_TYPE_CLIENT}"


def organization(alias: str, *, name: str | None = None, attributes: object = None) -> dict:
    return {
        "id": f"id-of-{alias}",
        "name": name if name is not None else alias.title(),
        "alias": alias,
        "enabled": True,
        "attributes": {} if attributes is None else attributes,
        "domains": [],
    }


class AdminApi:
    """A stand-in Admin API: canned answers, every request recorded."""

    def __init__(
        self,
        organizations: object = (),
        *,
        token_status: int = 200,
        lookup_status: int = 200,
        token_body: object = None,
        lookup_body: bytes | None = None,
        transport_error: bool = False,
    ) -> None:
        self.organizations = list(organizations) if isinstance(organizations, (list, tuple)) else organizations
        self.token_status = token_status
        self.lookup_status = lookup_status
        self.token_body = token_body
        self.lookup_body = lookup_body
        self.transport_error = transport_error
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.transport_error:
            raise httpx.ConnectError("connection refused", request=request)
        if str(request.url) == TOKEN_URL:
            body = (
                self.token_body
                if self.token_body is not None
                else {"access_token": SERVICE_ACCOUNT_TOKEN, "expires_in": 300, "token_type": "Bearer"}
            )
            return httpx.Response(self.token_status, json=body)
        if self.lookup_body is not None:
            return httpx.Response(self.lookup_status, content=self.lookup_body)
        return httpx.Response(self.lookup_status, json=self.organizations)

    def directory(self, *, issuer: str = ISSUER, secret: str = DIRECTORY_CLIENT_SECRET) -> KeycloakDirectory:
        return KeycloakDirectory(
            issuer=issuer,
            client_id=DIRECTORY_CLIENT_ID,
            client_secret=secret,
            client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(self.handle), timeout=DIRECTORY_TIMEOUT_SECONDS
            ),
        )

    def lookups(self) -> list[httpx.Request]:
        return [request for request in self.requests if str(request.url) != TOKEN_URL]

    def grants(self) -> list[httpx.Request]:
        return [request for request in self.requests if str(request.url) == TOKEN_URL]
