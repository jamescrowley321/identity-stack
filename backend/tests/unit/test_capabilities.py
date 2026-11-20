"""Adapter capabilities, ``GET /api/capabilities``, and the 404 of a route whose capability is missing."""

import uuid
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Ok
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies import identity, rbac
from app.dependencies.capabilities import (
    NOTHING,
    Served,
    require_custom_attributes,
    require_machine_credentials,
    require_relationship_store,
)
from app.dependencies.identity import (
    SYNC_ADAPTERS,
    SyncAdapterRegistration,
    get_identity_resolution_service,
    get_user_service,
    relationship_store_kind,
    sync_capabilities,
)
from app.dependencies.rbac import get_rbac_session
from app.dependencies.tenant import get_tenant_id
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import descope_config, keycloak_config, ory_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from app.services.adapters.base import Capability, IdentityProviderAdapter, TenantAttributes
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.adapters.ory import OrySyncAdapter
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.relationship_store import RelationshipStoreKind
from app.services.user import UserService
from tests.unit.test_descope_backed_routes import (
    DESCOPE_ISSUER,
    DESCOPE_MEMBER,
    DESCOPE_PROJECT,
    KEY_BODY,
    ORY_AUDIENCE,
    ORY_ISSUER,
    ORY_OWNER,
    TENANT_A,
    _bearer,
    _url,
)

DESCOPE_CAPABILITIES = frozenset({Capability.MACHINE_CREDENTIALS, Capability.CUSTOM_ATTRIBUTES})
KEYCLOAK_CAPABILITIES = frozenset({Capability.MACHINE_CREDENTIALS, Capability.CUSTOM_ATTRIBUTES})

KEYCLOAK_ISSUER = "https://keycloak.example.test/realms/r"
KEYCLOAK_AUDIENCE = "identity-stack-api"
KEYCLOAK_SPA = "identity-stack-spa"
KEYCLOAK_MEMBER = {
    "iss": KEYCLOAK_ISSUER,
    "sub": "keycloak-subject-1",
    "aud": KEYCLOAK_AUDIENCE,
    "azp": KEYCLOAK_SPA,
    "typ": "Bearer",
}

# Each adapter's own declaration: what it implements today. Every adapter class
# in the application must be listed here (``test_every_adapter_is_listed``).
DECLARED = {
    DescopeSyncAdapter: DESCOPE_CAPABILITIES,
    KeycloakSyncAdapter: KEYCLOAK_CAPABILITIES,
    ZitadelSyncAdapter: frozenset(),
    OrySyncAdapter: frozenset(),
    NoOpSyncAdapter: frozenset(),
}

DESCOPE_SERVED = Served(DESCOPE_CAPABILITIES, RelationshipStoreKind.DESCOPE, sync_provider_principal=True)
# Keycloak's own principal is served every capability its adapter declares; OpenFGA
# serves it only where CANONICAL_RBAC_PROVIDERS names Keycloak, which here it does not
# (the default, ``ory``). Keycloak's machine credentials are canonical: they serve a
# principal authorized from the canonical model, never a Descope principal (#680); its
# custom attributes take the caller's sub and tenant as Keycloak's ids, so they serve a
# Keycloak principal only (#679).
KEYCLOAK_SERVED = Served(KEYCLOAK_CAPABILITIES, RelationshipStoreKind.NONE, sync_provider_principal=True)
KEYCLOAK_MACHINE_CREDENTIALS = frozenset({Capability.MACHINE_CREDENTIALS})

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
        ("PATCH", "/api/profile/attributes"),
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
        "sync_provider_principal": served.sync_provider_principal,
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
        GatewayClaimsMiddleware,
        providers=[
            descope_config(DESCOPE_PROJECT),
            ory_config(ORY_ISSUER, ORY_AUDIENCE),
            keycloak_config(KEYCLOAK_ISSUER, KEYCLOAK_AUDIENCE, KEYCLOAK_SPA),
        ],
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

    def test_only_keycloaks_machine_credentials_serve_every_canonical_principal(self):
        assert {adapter: adapter.canonical_capabilities for adapter in DECLARED} == {
            **{adapter: frozenset() for adapter in DECLARED},
            KeycloakSyncAdapter: KEYCLOAK_MACHINE_CREDENTIALS,
        }

    def test_an_adapter_whose_canonical_capabilities_it_does_not_declare_is_refused(self):
        with pytest.raises(TypeError, match="Overreaching declares canonical_capabilities it does not declare"):
            type(
                "Overreaching",
                (IdentityProviderAdapter,),
                {"capabilities": frozenset(), "canonical_capabilities": KEYCLOAK_MACHINE_CREDENTIALS},
            )

    def test_an_adapter_that_declares_none_is_refused(self):
        with pytest.raises(TypeError, match="Undeclared must declare capabilities"):
            type("Undeclared", (IdentityProviderAdapter,), {})

    def test_an_adapter_that_declares_custom_attributes_without_them_is_refused(self):
        with pytest.raises(TypeError, match="Unimplemented declares custom_attributes but is no CustomAttributes"):
            type(
                "Unimplemented",
                (IdentityProviderAdapter,),
                {"capabilities": frozenset({Capability.CUSTOM_ATTRIBUTES})},
            )

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
            ("keycloak", KEYCLOAK_CAPABILITIES, RelationshipStoreKind.OPENFGA),
            ("zitadel", frozenset(), RelationshipStoreKind.OPENFGA),
        ],
    )
    def test_the_deployment_has_its_sync_adapters_capabilities(self, monkeypatch, provider, capabilities, store):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", "")

        assert (sync_capabilities(), relationship_store_kind()) == (capabilities, store)

    def test_a_provider_has_machine_credentials_exactly_when_its_adapter_declares_them(self):
        """The registration's builder and the adapter's declaration are one decision."""
        assert {name: registration.machine_credentials is not None for name, registration in SYNC_ADAPTERS.items()} == {
            name: Capability.MACHINE_CREDENTIALS in registration.adapter.capabilities
            for name, registration in SYNC_ADAPTERS.items()
        }


OPENFGA_SERVED = Served(frozenset(), RelationshipStoreKind.OPENFGA)

# (sync provider, RELATIONSHIP_STORE, principal) -> what the caller is served.
# An adapter's capabilities take the caller's sub and tenant as its provider's ids,
# so they serve that provider's own principals only, except Keycloak's canonical
# machine credentials, which serve every canonical principal. Descope's store is keyed on
# Descope ids: it serves a Descope principal only. OpenFGA is keyed on canonical
# ids: it serves a principal of a provider in CANONICAL_RBAC_PROVIDERS only.
SERVED_TO = [
    ("descope", "", "descope", DESCOPE_SERVED),
    ("descope", "", "ory", NOTHING),
    ("descope", "", "keycloak", NOTHING),
    ("keycloak", "", "keycloak", KEYCLOAK_SERVED),
    ("keycloak", "", "descope", NOTHING),
    ("keycloak", "", "ory", Served(KEYCLOAK_MACHINE_CREDENTIALS, RelationshipStoreKind.OPENFGA)),
    ("zitadel", "", "descope", NOTHING),
    ("zitadel", "", "ory", OPENFGA_SERVED),
    ("zitadel", "", "keycloak", NOTHING),
    ("descope", "openfga", "descope", Served(DESCOPE_CAPABILITIES, RelationshipStoreKind.NONE, True)),
    ("descope", "openfga", "ory", OPENFGA_SERVED),
    ("keycloak", "none", "ory", Served(KEYCLOAK_MACHINE_CREDENTIALS, RelationshipStoreKind.NONE)),
    ("keycloak", "descope", "ory", Served(KEYCLOAK_MACHINE_CREDENTIALS, RelationshipStoreKind.NONE)),
]
PRINCIPALS = {"descope": DESCOPE_MEMBER, "ory": ORY_OWNER, "keycloak": KEYCLOAK_MEMBER}


class TestTheCapabilitiesRoute:
    @pytest.mark.parametrize(("provider", "store", "principal", "expected"), SERVED_TO)
    def test_reports_what_serves_the_caller(self, client, monkeypatch, provider, store, principal, expected):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", store)

        response = client.get("/api/capabilities", headers=_bearer(PRINCIPALS[principal]))

        assert (response.status_code, response.json()) == (200, _response(expected))

    @pytest.mark.parametrize(("provider", "store", "principal", "expected"), SERVED_TO)
    def test_its_sync_provider_flag_is_the_sync_provider_guards_decision(
        self, client, monkeypatch, provider, store, principal, expected
    ):
        """The SPA shows Members and Roles by the flag: it must be exactly whom the guard lets through."""
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", store)
        headers = _bearer(PRINCIPALS[principal])

        flag = client.get("/api/capabilities", headers=headers).json()["sync_provider_principal"]
        members = client.get("/api/members", headers=headers)
        fenced = members.status_code == 403 and members.json().get("detail", "").startswith("This route serves ")

        assert (flag, not fenced) == (expected.sync_provider_principal, expected.sync_provider_principal)

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


# What gives each gated route, read from what the caller is served.
GIVEN_BY = {
    **{
        route: (lambda served: Capability.MACHINE_CREDENTIALS in served.capabilities)
        for route in MACHINE_CREDENTIAL_ROUTES
    },
    **{
        route: (lambda served: Capability.CUSTOM_ATTRIBUTES in served.capabilities) for route in CUSTOM_ATTRIBUTE_ROUTES
    },
    **{
        route: (lambda served: served.relationship_store is not RelationshipStoreKind.NONE)
        for route in RELATIONSHIP_STORE_ROUTES
    },
}
# (sync provider, RELATIONSHIP_STORE, principal, method, path) of every gated route a caller is not served.
MISSING_FOR = sorted(
    (provider, store, principal, method, path)
    for provider, store, principal, served in SERVED_TO
    for (method, path) in EVERY_GATED_ROUTE
    if not GIVEN_BY[(method, path)](served)
)


# A Descope owner and admin whose current tenant is a canonical tenant's id.
DESCOPE_CANONICAL_TENANT_ADMIN = {
    "iss": DESCOPE_ISSUER,
    "sub": "descope-admin",
    "dct": TENANT_A,
    "tenants": {TENANT_A: {"roles": ["owner", "admin"], "permissions": []}},
}


class TestAMissingCapabilityIs404:
    """Every gated route, for every caller the capability does not serve (``SERVED_TO``)."""

    @pytest.mark.parametrize(("provider", "store", "principal", "method", "path"), MISSING_FOR)
    def test_on_every_gated_route(self, client, monkeypatch, descope_client, method, path, provider, store, principal):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", provider)
        monkeypatch.setattr(identity, "RELATIONSHIP_STORE", store)
        url = _url(path)

        response = client.request(method, url, headers=_bearer(PRINCIPALS[principal]))

        assert (response.status_code, response.json()) == (404, _missing(url, MISSING_DETAIL[(method, path)]))
        assert descope_client.mock_calls == []

    @pytest.mark.parametrize(("method", "path"), sorted(MACHINE_CREDENTIAL_ROUTES))
    def test_a_descope_admin_naming_a_canonical_tenant_gets_no_keycloak_machine_credentials(
        self, client, monkeypatch, method, path
    ):
        """A Descope token's ``dct`` naming a canonical tenant's id reaches no canonical ``/api/keys``."""
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
        built = []
        client.app.dependency_overrides[identity.get_machine_credential_service] = lambda: built.append(path)
        url = _url(path)

        response = client.request(method, url, headers=_bearer(DESCOPE_CANONICAL_TENANT_ADMIN), json=KEY_BODY)
        served = client.get("/api/capabilities", headers=_bearer(DESCOPE_CANONICAL_TENANT_ADMIN))

        assert (response.status_code, response.json()) == (404, _missing(url, MISSING_DETAIL[(method, path)]))
        assert (served.status_code, served.json()) == (200, _response(NOTHING))
        assert built == []

    @pytest.mark.parametrize(("method", "path"), sorted(EVERY_GATED_ROUTE))
    def test_a_request_with_no_token_still_gets_401(self, client, method, path):
        assert client.request(method, _url(path)).status_code == 401

    def test_the_profile_still_loads(self, client, monkeypatch):
        """Every session loads it: name and email are the canonical user's, not a capability."""
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        resolver = AsyncMock()
        resolver.resolve.return_value = Ok({"user": CANONICAL_USER})
        client.app.dependency_overrides[get_identity_resolution_service] = lambda: resolver

        response = client.get("/api/profile", headers=_bearer(ORY_OWNER))

        assert (response.status_code, response.json()) == (200, CANONICAL_PROFILE)
        resolver.resolve.assert_awaited_once_with(provider="ory", sub=ORY_OWNER["sub"])


CANONICAL_USER = {
    "id": "0b1d0b1d-0000-4000-8000-000000000001",
    "email": "member@example.test",
    "user_name": "member",
    "given_name": "A",
    "family_name": "Member",
    "status": "active",
}
CANONICAL_PROFILE = {
    "user_id": ORY_OWNER["sub"],
    "name": "A Member",
    "given_name": "A",
    "family_name": "Member",
    "email": "member@example.test",
    "custom_attributes": {},
}
TENANT = "11111111-1111-1111-1111-111111111111"


class TestAProvidersOwnPrincipalReachesItsAdapter:
    """A Keycloak principal of a Keycloak deployment: the custom attribute routes go to the sync adapter."""

    @pytest.fixture
    def adapter(self, monkeypatch) -> AsyncMock:
        adapter = AsyncMock(spec=KeycloakSyncAdapter)
        registration = SYNC_ADAPTERS["keycloak"]
        monkeypatch.setitem(
            SYNC_ADAPTERS,
            "keycloak",
            SyncAdapterRegistration(registration.adapter, lambda _state, _session: adapter, registration.own_principal),
        )
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        return adapter

    def test_the_profile_reads_its_attributes_by_the_callers_subject(self, client, adapter):
        resolver = AsyncMock()
        resolver.resolve.return_value = Ok({"user": CANONICAL_USER})
        client.app.dependency_overrides[get_identity_resolution_service] = lambda: resolver
        adapter.user_attributes.return_value = Ok({"department": "Engineering", "unlisted": "kept out"})

        response = client.get("/api/profile", headers=_bearer(KEYCLOAK_MEMBER))

        assert (response.status_code, response.json()) == (
            200,
            {
                **CANONICAL_PROFILE,
                "user_id": KEYCLOAK_MEMBER["sub"],
                "custom_attributes": {"department": "Engineering"},
            },
        )
        adapter.user_attributes.assert_awaited_once_with(subject=KEYCLOAK_MEMBER["sub"])
        resolver.resolve.assert_awaited_once_with(provider="keycloak", sub=KEYCLOAK_MEMBER["sub"])

    def test_the_profile_loads_when_the_adapter_cannot_be_built(self, client, monkeypatch):
        """The profile is informational: an adapter that fails to build leaves it without custom attributes."""

        def broken(_state, _session):
            raise RuntimeError("Keycloak admin client not initialized")

        registration = SYNC_ADAPTERS["keycloak"]
        monkeypatch.setitem(
            SYNC_ADAPTERS, "keycloak", SyncAdapterRegistration(registration.adapter, broken, registration.own_principal)
        )
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        resolver = AsyncMock()
        resolver.resolve.return_value = Ok({"user": CANONICAL_USER})
        client.app.dependency_overrides[get_identity_resolution_service] = lambda: resolver

        response = client.get("/api/profile", headers=_bearer(KEYCLOAK_MEMBER))

        assert (response.status_code, response.json()) == (
            200,
            {**CANONICAL_PROFILE, "user_id": KEYCLOAK_MEMBER["sub"]},
        )

    def test_the_profile_writes_an_attribute_by_the_callers_subject(self, client, adapter):
        adapter.set_user_attribute.return_value = Ok(None)

        response = client.patch(
            "/api/profile/attributes",
            json={"key": "department", "value": "Engineering"},
            headers=_bearer(KEYCLOAK_MEMBER),
        )

        assert (response.status_code, response.json()) == (
            200,
            {"status": "updated", "key": "department", "value": "Engineering"},
        )
        adapter.set_user_attribute.assert_awaited_once_with(
            subject=KEYCLOAK_MEMBER["sub"], key="department", value="Engineering"
        )

    def test_the_tenant_settings_read_the_resolved_tenant(self, client, adapter):
        tenant = TENANT
        client.app.dependency_overrides[get_tenant_id] = lambda: tenant
        adapter.tenant_attributes.return_value = Ok(TenantAttributes("Acme", {"plan_tier": "pro", "other": 1}))

        response = client.get("/api/tenants/current/settings", headers=_bearer(KEYCLOAK_MEMBER))

        assert (response.status_code, response.json()) == (
            200,
            {"tenant_id": tenant, "name": "Acme", "custom_attributes": {"plan_tier": "pro"}},
        )
        adapter.tenant_attributes.assert_awaited_once_with(tenant=tenant)

    def test_a_tenant_the_provider_lacks_has_empty_settings(self, client, adapter):
        tenant = TENANT
        client.app.dependency_overrides[get_tenant_id] = lambda: tenant
        adapter.tenant_attributes.return_value = Ok(None)

        response = client.get("/api/tenants/current/settings", headers=_bearer(KEYCLOAK_MEMBER))

        assert (response.status_code, response.json()) == (
            200,
            {"tenant_id": tenant, "name": "", "custom_attributes": {}},
        )

    @pytest.fixture
    def users(self, client) -> AsyncMock:
        users = AsyncMock(spec=UserService)
        users.update_own_names.return_value = Ok({**CANONICAL_USER, "given_name": "B", "family_name": "Other"})
        resolver = AsyncMock()
        resolver.resolve.return_value = Ok({"user": CANONICAL_USER})
        client.app.dependency_overrides[get_identity_resolution_service] = lambda: resolver
        client.app.dependency_overrides[get_user_service] = lambda: users
        return users

    @pytest.mark.parametrize(
        ("principal", "subject"),
        [("keycloak", KEYCLOAK_MEMBER["sub"]), ("ory", None), ("descope", None)],
    )
    def test_names_are_saved_for_every_provider_and_pushed_to_the_sync_providers_own_principal_only(
        self, client, adapter, users, principal, subject
    ):
        response = client.patch(
            "/api/profile", json={"given_name": "B", "family_name": "Other"}, headers=_bearer(PRINCIPALS[principal])
        )

        assert (response.status_code, response.json()) == (
            200,
            {"status": "updated", "name": "B Other", "given_name": "B", "family_name": "Other"},
        )
        users.update_own_names.assert_awaited_once_with(
            user_id=uuid.UUID(CANONICAL_USER["id"]), given_name="B", family_name="Other", subject=subject
        )


class TestKeycloakTenantSettingsNeedAnAdminRole:
    """``PATCH /api/tenants/current/settings`` for a Keycloak principal, authorized from the canonical model."""

    @pytest.fixture
    def adapter(self, monkeypatch, client) -> AsyncMock:
        adapter = AsyncMock(spec=KeycloakSyncAdapter)
        adapter.set_tenant_attributes.return_value = Ok(None)
        registration = SYNC_ADAPTERS["keycloak"]
        monkeypatch.setitem(
            SYNC_ADAPTERS,
            "keycloak",
            SyncAdapterRegistration(registration.adapter, lambda _state, _session: adapter, registration.own_principal),
        )
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"keycloak"}))

        async def _session():
            yield MagicMock()

        client.app.dependency_overrides[get_rbac_session] = _session
        return adapter

    def _held(self, monkeypatch, role: str) -> None:
        resolver = AsyncMock()
        resolver.resolve.return_value = Ok(
            {"user": CANONICAL_USER, "roles": [{"tenant_id": TENANT, "role_name": role, "permissions": []}]}
        )
        monkeypatch.setattr(rbac, "IdentityResolutionService", lambda **_: resolver)

    def test_an_admin_saves(self, client, adapter, monkeypatch):
        self._held(monkeypatch, "admin")

        response = client.patch(
            "/api/tenants/current/settings",
            json={"custom_attributes": {"plan_tier": "pro"}},
            headers=_bearer(KEYCLOAK_MEMBER),
        )

        assert (response.status_code, response.json()) == (
            200,
            {"status": "updated", "tenant_id": TENANT, "custom_attributes": {"plan_tier": "pro"}},
        )
        adapter.set_tenant_attributes.assert_awaited_once_with(tenant=TENANT, attributes={"plan_tier": "pro"})

    def test_a_member_is_refused(self, client, adapter, monkeypatch):
        self._held(monkeypatch, "member")

        response = client.patch(
            "/api/tenants/current/settings",
            json={"custom_attributes": {"plan_tier": "pro"}},
            headers=_bearer(KEYCLOAK_MEMBER),
        )

        assert (response.status_code, response.json()) == (403, {"detail": "Insufficient permissions"})
        adapter.set_tenant_attributes.assert_not_awaited()
