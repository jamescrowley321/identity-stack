"""The two role-gated access checks answer a canonical principal from its roles in the chosen tenant.

The principal is a Zitadel one built from the recorded SPA token
(``tests/fixtures/zitadel/``), and the canonical answer is stubbed at
``IdentityResolutionService``, as in ``test_zitadel_canonical_roles.py``: admin in
one tenant, viewer in another. The tenant is chosen with ``X-Tenant-ID``.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from expression import Ok
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.dependencies import rbac
from app.dependencies.rbac import get_rbac_session
from app.middleware.providers import zitadel_config
from app.routers import access_checks

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zitadel"
ISSUER = "http://zitadel.localhost:8480"
PROJECT_ID = "399781183140937731"
SPA_CLIENT_ID = "399781187016540163"
ZITADEL = zitadel_config(ISSUER, PROJECT_ID, SPA_CLIENT_ID, require_https=False)
CLAIMS = json.loads((FIXTURES / "human-access-token-project.json").read_text())["claims"]

ACME = "11111111-1111-1111-1111-111111111111"
GLOBEX = "22222222-2222-2222-2222-222222222222"
ADMIN_ROUTE = "/api/access-checks/admin-role"
VIEWER_ROUTE = "/api/access-checks/viewer-role"
INSUFFICIENT_PERMISSIONS = {"detail": "Insufficient permissions"}
NO_TENANT_CONTEXT = {"detail": "No tenant context"}


class _Principal(BaseHTTPMiddleware):
    """Stands in for the token middleware: the request carries the recorded Zitadel principal."""

    async def dispatch(self, request, call_next):
        request.state.claims = CLAIMS
        request.state.provider = ZITADEL
        principal = MagicMock()
        principal.identity.authentication_type = ZITADEL.name
        request.state.principal = principal
        return await call_next(request)


class _StubResolver:
    def __init__(self, result):
        self._result = result

    async def resolve(self, *, provider: str, sub: str):
        return self._result


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"zitadel"}))
    resolver = _StubResolver(
        Ok(
            {
                "user": {"id": "canonical-user-1", "status": "active"},
                "roles": [
                    {"tenant_id": ACME, "role_name": "admin", "permissions": []},
                    {"tenant_id": GLOBEX, "role_name": "viewer", "permissions": []},
                ],
            }
        )
    )
    monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: resolver)

    app = FastAPI()
    app.include_router(access_checks.router, prefix="/api")
    app.dependency_overrides[get_rbac_session] = lambda: MagicMock()
    app.add_middleware(_Principal)
    return TestClient(app)


def _in(tenant: str) -> dict[str, str]:
    return {"X-Tenant-ID": tenant}


class TestTheAdminCheck:
    def test_an_admin_of_the_chosen_tenant_is_answered_with_their_roles(self, client):
        response = client.get(ADMIN_ROUTE, headers=_in(ACME))

        assert (response.status_code, response.json()) == (200, {"roles": ["admin"]})

    def test_a_viewer_of_the_chosen_tenant_is_refused(self, client):
        response = client.get(ADMIN_ROUTE, headers=_in(GLOBEX))

        assert (response.status_code, response.json()) == (403, INSUFFICIENT_PERMISSIONS)


class TestTheViewerCheck:
    @pytest.mark.parametrize(("tenant", "roles"), [(ACME, ["admin"]), (GLOBEX, ["viewer"])], ids=["admin", "viewer"])
    def test_any_canonical_role_in_the_chosen_tenant_is_answered(self, client, tenant, roles):
        response = client.get(VIEWER_ROUTE, headers=_in(tenant))

        assert (response.status_code, response.json()) == (200, {"roles": roles})

    def test_a_tenant_the_caller_is_not_in_is_refused(self, client):
        response = client.get(VIEWER_ROUTE, headers=_in("33333333-3333-3333-3333-333333333333"))

        assert (response.status_code, response.json()) == (403, NO_TENANT_CONTEXT)
