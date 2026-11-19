"""Adapter capabilities, ``GET /api/capabilities``, and the 404 of a route whose capability is missing."""

from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies import identity
from app.dependencies.capabilities import (
    NOTHING,
    Served,
    require_custom_attributes,
    require_machine_credentials,
    require_relationship_store,
)
from app.dependencies.identity import SYNC_ADAPTERS, relationship_store_kind, sync_capabilities
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import descope_config, ory_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from app.services.adapters.base import Capability, IdentityProviderAdapter
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.adapters.ory import OrySyncAdapter
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.relationship_store import RelationshipStoreKind
from tests.unit.test_descope_backed_routes import (
    DESCOPE_MEMBER,
    DESCOPE_PROJECT,
    ORY_AUDIENCE,
    ORY_ISSUER,
    ORY_OWNER,
    _bearer,
    _url,
)

DESCOPE_CAPABILITIES = frozenset({Capability.MACHINE_CREDENTIALS, Capability.CUSTOM_ATTRIBUTES})

# Each adapter's own declaration: what it implements today. Every adapter class
# in the application must be listed here (``test_every_adapter_is_listed``).
DECLARED = {
    DescopeSyncAdapter: DESCOPE_CAPABILITIES,
    KeycloakSyncAdapter: frozenset(),
    ZitadelSyncAdapter: frozenset(),
    OrySyncAdapter: frozenset(),
    NoOpSyncAdapter: frozenset(),
}

DESCOPE_SERVED = Served(DESCOPE_CAPABILITIES, RelationshipStoreKind.DESCOPE)

# (method, path) of every route each capability serves.
MACHINE_CREDENTIAL_ROUTES = frozenset(
    {
        ("POST", "/api/keys"),
        ("GET", "/api/keys"),
        ("GET", "/api/keys/{key_id}"),
        ("POST", "/api/keys/{key_id}/deactivate"),
        ("POST", "/api/keys/{key_id}/activate"),
        ("DELETE", "/api/keys/{key_id}"),
    }
)
CUSTOM_ATTRIBUTE_ROUTES = frozenset(
    {
        ("PATCH", "/api/profile"),
        ("GET", "/api/tenants/current/settings"),
        ("PATCH", "/api/tenants/current/settings"),
    }
)
RELATIONSHIP_STORE_ROUTES = frozenset(
    {
        ("GET", "/api/fga/schema"),
        ("PUT", "/api/fga/schema"),
        ("POST", "/api/fga/relations"),
        ("DELETE", "/api/fga/relations"),
        ("GET", "/api/fga/relations"),
        ("POST", "/api/fga/check"),
        ("POST", "/api/documents"),
        ("GET", "/api/documents"),
        ("GET", "/api/documents/{document_id}"),
        ("PUT", "/api/documents/{document_id}"),
        ("DELETE", "/api/documents/{document_id}"),
        ("POST", "/api/documents/{document_id}/share"),
        ("DELETE", "/api/documents/{document_id}/share/{target_user_id}"),
    }
)
GATED = {
    require_machine_credentials: MACHINE_CREDENTIAL_ROUTES,
    require_custom_attributes: CUSTOM_ATTRIBUTE_ROUTES,
    require_relationship_store: RELATIONSHIP_STORE_ROUTES,
}
EVERY_GATED_ROUTE = MACHINE_CREDENTIAL_ROUTES | CUSTOM_ATTRIBUTE_ROUTES | RELATIONSHIP_STORE_ROUTES


def _dependency_calls(dependant: Dependant) -> Iterator[Callable]:
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from _dependency_calls(sub)


def _shipped_routes() -> Iterator[tuple[str, APIRoute]]:
    for route in shipped_app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                yield method, route


def _response(served: Served) -> dict:
    return {
        "capabilities": sorted(capability.value for capability in served.capabilities),
        "relationship_store": served.relationship_store.value,
    }


def _missing(path: str, detail: str) -> dict:
    return {
        "type": "/errors/capability-missing",
        "title": "Capability Not Available",
        "status": 404,
        "detail": detail,
        "instance": path,
    }


@pytest.fixture
def descope_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def client(descope_client) -> TestClient:
    """Every shipped route behind the shipped gateway middleware."""
    limiter.reset()
    app = FastAPI()
    app.state.limiter = limiter
    app.state.descope_client = descope_client
    app.state.cache_publisher = MagicMock()
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    for exception, handler in shipped_app.exception_handlers.items():
        app.add_exception_handler(exception, handler)
    app.include_router(shipped_app.router)

    async def _session():
        yield MagicMock()

    app.dependency_overrides[get_async_session] = _session
    app.add_middleware(
        GatewayClaimsMiddleware, providers=[descope_config(DESCOPE_PROJECT), ory_config(ORY_ISSUER, ORY_AUDIENCE)]
    )
    return TestClient(app)


def _adapter_classes(base: type = IdentityProviderAdapter) -> set[type]:
    found = set()
    for adapter in base.__subclasses__():
        if adapter.__module__.startswith("app."):
            found |= {adapter} | _adapter_classes(adapter)
    return found


class TestDeclarations:
    def test_every_adapter_is_listed(self):
        assert _adapter_classes() == set(DECLARED)

    def test_each_adapter_declares_its_own_set(self):
        assert {adapter: vars(adapter)["capabilities"] for adapter in DECLARED} == DECLARED

    def test_an_adapter_that_declares_none_is_refused(self):
        with pytest.raises(TypeError, match="Undeclared must declare capabilities"):
            type("Undeclared", (IdentityProviderAdapter,), {})

    def test_every_registration_names_its_adapter(self):
        assert {name: registration.adapter for name, registration in SYNC_ADAPTERS.items()} == {
            "descope": DescopeSyncAdapter,
            "keycloak": KeycloakSyncAdapter,
            "zitadel": ZitadelSyncAdapter,
        }

    @pytest.mark.parametrize(
        ("provider", "capabilities", "store"),
        [
            ("descope", DESCOPE_CAPABILITIES, RelationshipStoreKind.DESCOPE),
            ("keycloak", frozenset(), RelationshipStoreKind.NONE),
            ("zitadel", frozenset(), RelationshipStoreKind.NONE),
        ],
    )
    def test_the_deployment_has_its_sync_adapters_capabilities(self, monkeypatch, provider, capabilities, store):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)

        assert (sync_capabilities(), relationship_store_kind()) == (capabilities, store)


# (sync provider, principal) -> what the caller is served. Descope's capabilities and
# store are keyed on Descope ids: they serve a Descope principal only.
SERVED_TO = [
    ("descope", "descope", DESCOPE_SERVED),
    ("descope", "ory", NOTHING),
    ("keycloak", "descope", NOTHING),
    ("keycloak", "ory", NOTHING),
    ("zitadel", "descope", NOTHING),
    ("zitadel", "ory", NOTHING),
]
PRINCIPALS = {"descope": DESCOPE_MEMBER, "ory": ORY_OWNER}


class TestTheCapabilitiesRoute:
    @pytest.mark.parametrize(("provider", "principal", "expected"), SERVED_TO)
    def test_reports_what_serves_the_caller(self, client, monkeypatch, provider, principal, expected):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)

        response = client.get("/api/capabilities", headers=_bearer(PRINCIPALS[principal]))

        assert (response.status_code, response.json()) == (200, _response(expected))

    def test_a_request_with_no_token_gets_401(self, client):
        assert client.get("/api/capabilities").status_code == 401


class TestTheRegisters:
    @pytest.mark.parametrize("dependency", list(GATED), ids=lambda dependency: dependency.__name__)
    def test_each_capability_gates_exactly_its_routes(self, dependency):
        gated = {
            (method, route.path)
            for method, route in _shipped_routes()
            if dependency in _dependency_calls(route.dependant)
        }

        assert gated == GATED[dependency]

    def test_the_capability_is_decided_before_anything_else(self):
        """Before the Descope guard and the tenant resolver, so a missing capability is never their 403."""
        first = {
            (method, route.path): route.dependant.dependencies[0].call
            for method, route in _shipped_routes()
            if (method, route.path) in EVERY_GATED_ROUTE
        }

        assert first == {route: dependency for dependency, routes in GATED.items() for route in routes}


MISSING_DETAIL = {
    **{
        route: f"This deployment has no {Capability.MACHINE_CREDENTIALS} capability"
        for route in MACHINE_CREDENTIAL_ROUTES
    },
    **{route: f"This deployment has no {Capability.CUSTOM_ATTRIBUTES} capability" for route in CUSTOM_ATTRIBUTE_ROUTES},
    **{route: "This deployment has no relationship store" for route in RELATIONSHIP_STORE_ROUTES},
}


class TestAMissingCapabilityIs404:
    """Every gated route, for every caller the capability does not serve (``SERVED_TO``)."""

    @pytest.mark.parametrize(("method", "path"), sorted(EVERY_GATED_ROUTE))
    @pytest.mark.parametrize(
        ("provider", "principal"),
        [(provider, principal) for provider, principal, served in SERVED_TO if served == NOTHING],
    )
    def test_on_every_gated_route(self, client, monkeypatch, descope_client, method, path, provider, principal):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        url = _url(path)

        response = client.request(method, url, headers=_bearer(PRINCIPALS[principal]))

        assert (response.status_code, response.json()) == (404, _missing(url, MISSING_DETAIL[(method, path)]))
        assert descope_client.mock_calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(EVERY_GATED_ROUTE))
    def test_a_request_with_no_token_still_gets_401(self, client, method, path):
        assert client.request(method, _url(path)).status_code == 401

    def test_the_profile_still_loads(self, client, monkeypatch):
        """Every session loads it: name and email are not a capability."""
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")

        response = client.get("/api/profile", headers=_bearer(ORY_OWNER))

        assert (response.status_code, response.json()) == (
            200,
            {"user_id": ORY_OWNER["sub"], "name": "", "email": "", "custom_attributes": {}},
        )
