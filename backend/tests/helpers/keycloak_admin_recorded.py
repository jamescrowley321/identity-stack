"""The Keycloak Admin API as recorded from ``keycloak-test``, replayed one answer per request.

``tests/fixtures/keycloak_admin/responses.json`` holds the answers Keycloak 26.8.2
gave the admin adapter's service account (``identity-stack-admin``) in the
compose realm; ``tests/fixtures/keycloak_admin/README.md`` says how they were
taken. A test names the answers it expects, in order. Each request must be the
recorded one (method and path); its query and JSON body are kept for the test
to compare.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.services.keycloak_admin import KeycloakAdminApi, KeycloakAdminSettings

RECORDED = json.loads((Path(__file__).parents[1] / "fixtures" / "keycloak_admin" / "responses.json").read_text())

BASE = "http://keycloak-test.localhost:18180"
ISSUER = f"{BASE}/realms/identity-stack"
ADMIN_REALM = f"{BASE}/admin/realms/identity-stack"
ADMIN_CLIENT_ID = "identity-stack-admin"
# Local fixture value from the realm import.
ADMIN_CLIENT_SECRET = "local-only-admin-client"
# The API client, whose client roles are the canonical permissions and roles.
API_CLIENT_ID = "identity-stack-api"

# The canonical ids the recording pushed: the organization's alias, the user's
# username, and the ids the role and the permission client roles carry.
TENANT_ID = "7e5a0000-0000-4000-8000-000000000586"
USER_ID = "05e40000-0000-4000-8000-000000000586"
ROLE_ID = "40e00000-0000-4000-8000-000000000586"
PERMISSION_ID = "9e400000-0000-4000-8000-000000000586"
# The ids Keycloak gave them, read from the recorded ``Location`` headers.
ORGANIZATION_ID = RECORDED["organization_create"]["location"].rsplit("/", 1)[1]
KEYCLOAK_USER_ID = RECORDED["user_create"]["location"].rsplit("/", 1)[1]
RECORDED_TOKEN = RECORDED["token"]["body"]["access_token"]
# The admin client's startup check, as recorded: credential read, own account, its roles, the API client.
ROLE_CHECK = [
    "credential_check",
    "service_account_find",
    "service_account_role_mappings",
    "service_account_roles",
    "api_client_find",
]


def _expand(url: str) -> str:
    return url.replace("{base}", BASE)


def recorded_response(name: str) -> httpx.Response:
    entry = RECORDED[name]
    headers = {"location": _expand(entry["location"])} if "location" in entry else {}
    if entry["body"] is None:
        return httpx.Response(entry["status"], headers=headers)
    return httpx.Response(entry["status"], json=entry["body"], headers=headers)


@dataclass
class Sent:
    method: str
    path: str
    query: dict[str, str]
    body: object
    authorization: str | None
    raw_path: str = field(default="", compare=False)


@dataclass
class RecordedAdminApi:
    """Answers each request with the next recorded answer named in ``script``.

    An entry of ``script`` is a recorded answer's name, or an ``httpx.Response``
    for an answer Keycloak was not recorded giving, or an exception to raise.
    """

    script: list
    sent: list[Sent] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.script:
            raise AssertionError(f"no answer left for {request.method} {request.url}")
        step = self.script.pop(0)
        body = json.loads(request.content) if request.content and _is_json(request) else None
        self.sent.append(
            Sent(
                method=request.method,
                path=request.url.path,
                query=dict(request.url.params),
                body=body,
                authorization=request.headers.get("authorization"),
                raw_path=request.url.raw_path.decode().split("?", 1)[0],
            )
        )
        if isinstance(step, Exception):
            raise step
        if isinstance(step, httpx.Response):
            return step
        entry = RECORDED[step]
        recorded = urlsplit(_expand(entry["url"]))
        assert (request.method, request.url.path) == (entry["method"], recorded.path), step
        return recorded_response(step)

    def api(self, *, secret: str = ADMIN_CLIENT_SECRET, client_id: str = ADMIN_CLIENT_ID) -> KeycloakAdminApi:
        return KeycloakAdminApi(
            KeycloakAdminSettings(
                issuer=ISSUER, client_id=client_id, client_secret=secret, api_client_id=API_CLIENT_ID
            ),
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handle), timeout=5.0),
        )

    def calls(self) -> list[tuple[str, str]]:
        return [(sent.method, sent.path) for sent in self.sent]


def _is_json(request: httpx.Request) -> bool:
    return request.headers.get("content-type", "").startswith("application/json")


def admin_path(suffix: str) -> str:
    return urlsplit(f"{ADMIN_REALM}{suffix}").path


TOKEN_PATH = urlsplit(f"{ISSUER}/protocol/openid-connect/token").path
