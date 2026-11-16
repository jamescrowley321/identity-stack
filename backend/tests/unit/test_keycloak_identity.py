"""``GET /api/identity`` for a Keycloak principal, and what it leaves alone for Descope and Ory.

Token claims start from ``keycloak/fixtures/access-token.json``. The app is the
shipped routers behind the gateway middleware (which reads an unsigned
payload), with Keycloak's membership lookup and membership list reading a real
``KeycloakDirectory`` over a stand-in Admin API (``tests/helpers/keycloak_admin_api.py``).
The canonical services are doubles: the canonical identity is not what this
file is about.
"""

import ast
import asyncio
import base64
import json
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Error, Ok
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies import rbac
from app.dependencies.identity import get_identity_provisioning_service, get_identity_resolution_service
from app.dependencies.rbac import (
    MEMBERSHIP_LOOKUP_FAILED,
    MEMBERSHIPS_NOT_LISTED,
    TENANT_NOT_A_MEMBERSHIP,
    get_rbac_session,
)
from app.dependencies.tenant_access import NO_TENANT_CONTEXT, SOLE_MEMBERSHIP_NOT_CONFIRMED
from app.errors.identity import NotFound
from app.main import app as shipped_app
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import (
    CountedMemberships,
    KeycloakTenantMemberships,
    MembershipSourcesDisagree,
    ProviderTokenConfig,
    TenantMembership,
    TenantMembershipMistyped,
    descope_config,
    keycloak_config,
    ory_config,
)
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from app.routers import protected
from tests.helpers.keycloak_admin_api import ORGANIZATIONS_DISABLED_BODY, TENANT_ADMIN_MARKER, AdminApi, organization
from tests.helpers.keycloak_model import (
    DIRECTORY_CLIENT_SECRET,
    MEMBER_TYPE_CLIENT,
    TENANT_ADMIN_APP_CLIENT,
    TENANT_ADMIN_TYPE_CLIENT,
    directory_settings,
    model_settings,
)

FIXTURE = Path(__file__).resolve().parents[3] / "keycloak" / "fixtures" / "access-token.json"
FIXTURE_CLAIMS = json.loads(FIXTURE.read_text())["claims"]
ISSUER = FIXTURE_CLAIMS["iss"]
SUBJECT = FIXTURE_CLAIMS["sub"]
ORY_ISSUER = "https://ory.example.test"
ORY_AUDIENCE = "api"
DESCOPE_PROJECT = "P123"

ACME = "acme"
GLOBEX = "globex"
DOCUMENTS_READ = "documents:read"
DOCUMENTS_WRITE = "documents:write"
SETTINGS_MANAGE = "settings:manage"
IDENTITY = "/api/identity"
SETTINGS_MANAGE_ROUTE = "/api/access-checks/settings-manage"
SECONDS_OF_VALIDITY = 300

# What the canonical model holds for the caller: a user, a canonical tenant and
# role (one another provider's provisioning gave the user it linked), and the link.
CANONICAL = {
    "user": {
        "id": "8f0c6d4e-0000-4000-8000-00000000c0de",
        "email": "bob@globex.test",
        "user_name": "bob@globex.test",
        "given_name": "Bob",
        "family_name": "Two-Orgs",
        "status": "active",
    },
    "roles": [{"tenant_id": "0b5e0000-0000-4000-8000-0000000000d1", "role_name": "member", "permissions": []}],
    "tenant_memberships": [{"tenant_id": "0b5e0000-0000-4000-8000-0000000000d1", "tenant_name": "default"}],
    "linked_idps": [{"provider_name": "keycloak", "external_sub": SUBJECT}],
}


def _keycloak(api: AdminApi | None) -> ProviderTokenConfig:
    provider = keycloak_config(
        ISSUER,
        frozenset({MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT}),
        require_https=False,
        **model_settings(MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT),
    )
    if api is None:
        return provider
    memberships = KeycloakTenantMemberships(provider.tenant_claims.model, api.directory(issuer=ISSUER))
    return replace(provider, sole_membership_lookup=CountedMemberships(memberships), tenant_memberships=memberships)


def _member(**changes) -> dict:
    claims = {**FIXTURE_CLAIMS, "exp": int(time.time()) + SECONDS_OF_VALIDITY, **changes}
    return {name: value for name, value in claims.items() if value is not None}


def _tenant_admin(**changes) -> dict:
    return _member(
        **{
            "aud": TENANT_ADMIN_TYPE_CLIENT,
            "azp": TENANT_ADMIN_APP_CLIENT,
            "resource_access": {TENANT_ADMIN_TYPE_CLIENT: {"roles": [SETTINGS_MANAGE]}},
            **changes,
        }
    )


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _bearer(claims: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {_segment({'alg': 'RS256'})}.{_segment(claims)}.sig"}


class _Services:
    def __init__(self, identity: dict) -> None:
        self.resolver = AsyncMock()
        self.resolver.resolve.return_value = Ok(identity)
        self.provisioner = AsyncMock()


def _app(providers: list[ProviderTokenConfig], services: _Services) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.state.descope_client = AsyncMock()
    app.state.cache_publisher = MagicMock()
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(shipped_app.router)

    async def _session():
        yield MagicMock()

    app.dependency_overrides[get_rbac_session] = _session
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_identity_resolution_service] = lambda: services.resolver
    app.dependency_overrides[get_identity_provisioning_service] = lambda: services.provisioner
    app.add_middleware(GatewayClaimsMiddleware, providers=providers)
    return app


def _get(path: str, claims: dict, api: AdminApi | None, *, identity: dict = CANONICAL, **kwargs):
    limiter.reset()
    services = _Services(identity)
    client = TestClient(_app([descope_config(DESCOPE_PROJECT), _keycloak(api)], services))
    headers = {**_bearer(claims), **kwargs.pop("headers", {})}
    return client.get(path, headers=headers, **kwargs), services


def _assert(response, status: int, detail: str) -> None:
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}


def _acme_and_globex(**acme_attributes) -> AdminApi:
    return AdminApi(
        [
            organization(ACME, name="Acme", attributes=acme_attributes or None),
            organization(GLOBEX, name="Globex"),
        ]
    )


class TestAKeycloakPrincipalsIdentity:
    def test_the_tokens_organization_is_the_current_tenant_and_its_granted_roles_the_permissions(self):
        response, _ = _get(IDENTITY, _member(), AdminApi([organization(ACME, name="Acme")]))

        assert response.status_code == 200, response.text
        assert response.json() == {
            "user": CANONICAL["user"],
            "roles": [],
            "tenant_memberships": [{"tenant_id": ACME, "tenant_name": "Acme"}],
            "linked_idps": CANONICAL["linked_idps"],
            "token_tenant": {"tenant_id": ACME, "permissions": [DOCUMENTS_READ]},
        }

    def test_the_canonical_tenant_and_role_are_not_reported(self):
        response, _ = _get(IDENTITY, _member(), AdminApi([organization(ACME)]))

        body = response.json()
        assert body["roles"] == []
        assert {m["tenant_id"] for m in body["tenant_memberships"]} == {ACME}

    def test_a_user_in_two_organizations_sees_both(self):
        response, _ = _get(IDENTITY, _member(), _acme_and_globex())

        assert response.status_code == 200, response.text
        assert {m["tenant_id"]: m for m in response.json()["tenant_memberships"]} == {
            ACME: {"tenant_id": ACME, "tenant_name": "Acme"},
            GLOBEX: {"tenant_id": GLOBEX, "tenant_name": "Globex"},
        }

    def test_the_token_of_another_organization_reports_that_one(self):
        response, _ = _get(IDENTITY, _member(organization=[GLOBEX]), _acme_and_globex())

        assert response.json()["token_tenant"] == {"tenant_id": GLOBEX, "permissions": [DOCUMENTS_READ]}

    def test_a_first_login_is_provisioned_with_the_tokens_profile(self, monkeypatch):
        monkeypatch.setattr(protected, "JIT_ENABLED_PROVIDERS", {"ory", "keycloak"})
        response, services = _first_login()

        assert response.status_code == 200, response.text
        services.provisioner.provision.assert_awaited_once_with(
            provider_name="keycloak",
            sub=SUBJECT,
            email=FIXTURE_CLAIMS["email"],
            email_verified=FIXTURE_CLAIMS["email_verified"],
            given_name=FIXTURE_CLAIMS["given_name"],
            family_name=FIXTURE_CLAIMS["family_name"],
            assign_default_role=False,
        )


class TestKeycloakProvisioningIsOptIn:
    """Keycloak is not provisioned just in time unless the operator names it (Decision 6)."""

    def test_the_default_provisions_ory_only(self):
        """The knob's default as written in the router (read at import, so it is read from the source)."""
        defaults = {
            node.args[0].value: node.args[1].value
            for node in ast.walk(ast.parse(Path(protected.__file__).read_text()))
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "knob_set"
        }

        assert defaults["JIT_ENABLED_PROVIDERS"] == "ory"

    def test_a_first_login_is_not_provisioned_by_default(self, monkeypatch):
        monkeypatch.setattr(protected, "JIT_ENABLED_PROVIDERS", {"ory"})
        response, services = _first_login()

        _assert(response, 404, "Identity not found")
        services.provisioner.provision.assert_not_awaited()


def _first_login():
    """``GET /api/identity`` for a Keycloak subject with no canonical identity yet."""
    api = AdminApi([organization(ACME)])
    limiter.reset()
    services = _Services(CANONICAL)
    services.resolver.resolve.side_effect = [
        Error(NotFound("no canonical identity yet")),
        Ok(CANONICAL),
    ]
    services.provisioner.provision.return_value = Ok("8f0c6d4e-0000-4000-8000-00000000c0de")
    client = TestClient(_app([descope_config(DESCOPE_PROJECT), _keycloak(api)], services))
    return client.get(IDENTITY, headers=_bearer(_member())), services


class TestTheDirectoryIsBelievedOverTheToken:
    def test_the_tokens_organization_missing_from_the_list_is_403(self):
        """Decision 12: the membership changed after the token was issued."""
        response, _ = _get(IDENTITY, _member(), AdminApi([organization(GLOBEX)]))

        _assert(response, 403, TENANT_NOT_A_MEMBERSHIP)

    def test_a_user_in_no_organization_is_403(self):
        response, _ = _get(IDENTITY, _member(), AdminApi([]))

        _assert(response, 403, TENANT_NOT_A_MEMBERSHIP)

    def test_no_membership_list_wired_is_403(self):
        response, _ = _get(IDENTITY, _member(), None)

        _assert(response, 403, MEMBERSHIPS_NOT_LISTED)


class TestOneDirectoryReadPerRequest:
    """The confirmation of an organization-bound token and the membership list come from one read, under one limit."""

    @pytest.mark.parametrize("claims", [_member(), _tenant_admin()], ids=["member", "tenant-admin"])
    def test_get_identity_asks_the_admin_api_once(self, claims):
        api = AdminApi([organization(ACME, name="Acme", attributes={TENANT_ADMIN_MARKER: [SUBJECT]})])

        response, _ = _get(IDENTITY, claims, api)

        assert response.status_code == 200, response.text
        assert len(api.lookups()) == 1

    @pytest.mark.parametrize(
        ("organizations", "status"),
        [
            ([organization(ACME, name="Acme", attributes={TENANT_ADMIN_MARKER: [SUBJECT]})], 200),
            ([organization(ACME, name="Acme")], 403),
            ([organization(ACME, attributes={TENANT_ADMIN_MARKER: [SUBJECT]}), organization(GLOBEX)], 403),
        ],
        ids=["confirmed", "not-marked", "two-organizations"],
    )
    def test_the_confirmation_is_read_from_the_one_list(self, organizations, status):
        api = AdminApi(organizations)

        response, _ = _get(IDENTITY, _tenant_admin(), api)

        assert response.status_code == status, response.text
        assert len(api.lookups()) == 1
        if status == 403:
            assert response.json() == {"detail": SOLE_MEMBERSHIP_NOT_CONFIRMED}


class TestOneMembershipSource:
    """``GET /api/identity`` and a route that confirms the token's tenant read one source, so they cannot disagree."""

    @pytest.mark.parametrize(
        ("api", "status"),
        [
            (AdminApi([organization(ACME, name="Acme", attributes={TENANT_ADMIN_MARKER: [SUBJECT]})]), 200),
            (AdminApi([organization(ACME, name="Acme")]), 403),
            (AdminApi([organization(ACME, attributes={TENANT_ADMIN_MARKER: [SUBJECT]}), organization(GLOBEX)]), 403),
            (AdminApi([organization(GLOBEX, attributes={TENANT_ADMIN_MARKER: [SUBJECT]})]), 403),
            (AdminApi(lookup_status=404), 403),
        ],
        ids=["confirmed", "not-marked", "two-organizations", "another-organization", "member-unknown"],
    )
    def test_the_endpoint_and_a_lookup_route_give_the_same_answer(self, api, status):
        identity, _ = _get(IDENTITY, _tenant_admin(), api)
        settings, _ = _get(SETTINGS_MANAGE_ROUTE, _tenant_admin(), api)

        assert (identity.status_code, settings.status_code) == (status, status), (identity.text, settings.text)
        if status == 403:
            assert identity.json() == settings.json() == {"detail": SOLE_MEMBERSHIP_NOT_CONFIRMED}

    def test_keycloak_refuses_a_lookup_given_beside_its_directory(self):
        async def other(claims):
            return [ACME]

        with pytest.raises(MembershipSourcesDisagree):
            keycloak_config(
                ISSUER,
                frozenset({MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT}),
                require_https=False,
                organization_lookup=other,
                **model_settings(MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT),
                **directory_settings(),
            )

    @pytest.mark.parametrize("swap", ["lookup", "no-lookup", "list"])
    def test_a_provider_whose_lookup_is_not_its_list_is_refused(self, swap):
        async def other_lookup(claims):
            return [ACME]

        async def other_list(claims):
            return [TenantMembership(ACME, "Acme")]

        provider = _keycloak(AdminApi([organization(ACME)]))
        changes = {
            "lookup": {"sole_membership_lookup": other_lookup},
            "no-lookup": {"sole_membership_lookup": None},
            "list": {"tenant_memberships": other_list},
        }[swap]

        with pytest.raises(MembershipSourcesDisagree):
            replace(provider, **changes)

    def test_a_lookup_with_no_list_is_a_provider_of_its_own(self):
        async def lookup(claims):
            return [ACME]

        provider = replace(_keycloak(None), sole_membership_lookup=lookup)

        assert (provider.sole_membership_lookup, provider.tenant_memberships) == (lookup, None)


class TestAMembershipIsTyped:
    """A membership list entry is typed at construction, so the two readers of the list cannot count it differently."""

    @pytest.mark.parametrize(
        "fields",
        [
            {"counted": "false"},
            {"counted": "true"},
            {"counted": 1},
            {"counted": 0},
            {"counted": None},
            {"tenant_id": 7},
            {"tenant_id": None},
            {"tenant_name": None},
        ],
        ids=repr,
    )
    def test_a_field_of_the_wrong_type_is_refused(self, fields):
        with pytest.raises(TenantMembershipMistyped):
            TenantMembership(**{"tenant_id": ACME, "tenant_name": "Acme", **fields})

    @pytest.mark.parametrize("counted", [True, False], ids=repr)
    def test_a_well_typed_membership_is_kept(self, counted):
        assert TenantMembership(ACME, "Acme", counted) == TenantMembership(
            tenant_id=ACME, tenant_name="Acme", counted=counted
        )

    @pytest.mark.parametrize(
        "fields",
        [{"counted": "false"}, {"counted": 1}, {"tenant_id": 7}],
        ids=repr,
    )
    def test_the_endpoint_and_a_lookup_route_both_answer_502_for_a_mistyped_entry(self, fields):
        async def mistyped(claims):
            return [TenantMembership(**{"tenant_id": ACME, "tenant_name": "Acme", **fields})]

        provider = replace(
            _keycloak(None),
            sole_membership_lookup=CountedMemberships(mistyped),
            tenant_memberships=mistyped,
        )
        answers = []
        for path in (IDENTITY, SETTINGS_MANAGE_ROUTE):
            limiter.reset()
            client = TestClient(_app([descope_config(DESCOPE_PROJECT), provider], _Services(CANONICAL)))
            response = client.get(path, headers=_bearer(_tenant_admin()))
            answers.append((response.status_code, response.json()))

        assert answers == [(502, {"detail": MEMBERSHIP_LOOKUP_FAILED})] * 2


class TestAMemberTheRealmDoesNotKnow:
    """A user deleted from the realm while the token lives: the Admin API answers 404, the request 403."""

    @pytest.mark.parametrize(
        ("path", "claims", "detail"),
        [
            (IDENTITY, _member(), TENANT_NOT_A_MEMBERSHIP),
            (IDENTITY, _tenant_admin(), SOLE_MEMBERSHIP_NOT_CONFIRMED),
            (SETTINGS_MANAGE_ROUTE, _tenant_admin(), SOLE_MEMBERSHIP_NOT_CONFIRMED),
        ],
        ids=["identity-member", "identity-tenant-admin", "settings-manage"],
    )
    def test_is_refused_not_answered_502(self, path, claims, detail):
        response, services = _get(path, claims, AdminApi(lookup_status=404))

        _assert(response, 403, detail)
        services.provisioner.provision.assert_not_awaited()


class TestARealmFaultAnswered404Is502:
    """Organizations disabled in the realm: a 404 that is not an unknown member is a directory failure."""

    @pytest.mark.parametrize(
        ("path", "claims"),
        [(IDENTITY, _member()), (IDENTITY, _tenant_admin()), (SETTINGS_MANAGE_ROUTE, _tenant_admin())],
        ids=["identity-member", "identity-tenant-admin", "settings-manage"],
    )
    def test_is_502_not_a_refusal(self, path, claims):
        response, services = _get(path, claims, AdminApi(lookup_status=404, lookup_body=ORGANIZATIONS_DISABLED_BODY))

        _assert(response, 502, MEMBERSHIP_LOOKUP_FAILED)
        services.provisioner.provision.assert_not_awaited()


class TestAnUnreachableDirectoryIs502AndNoGrant:
    """Done when 3 and 8: never an empty list, never a 500, and the log has the status, not the secret."""

    @pytest.mark.parametrize(
        ("api", "status"),
        [
            (AdminApi(transport_error=True), None),
            (AdminApi(token_status=401), 401),
            (AdminApi(lookup_status=401), 401),
            (AdminApi(lookup_status=403), 403),
            (AdminApi(lookup_status=500), 500),
        ],
        ids=["unreachable", "grant-401", "lookup-401", "lookup-403", "lookup-500"],
    )
    @pytest.mark.parametrize(
        ("path", "claims"),
        [(IDENTITY, _member()), (IDENTITY, _tenant_admin()), (SETTINGS_MANAGE_ROUTE, _tenant_admin())],
        ids=["identity-member", "identity-tenant-admin", "settings-manage"],
    )
    def test_on_the_endpoint_and_on_an_organization_bound_permission_check(self, caplog, api, status, path, claims):
        api.organizations = [organization(ACME, attributes={TENANT_ADMIN_MARKER: [SUBJECT]})]

        with caplog.at_level("WARNING"):
            response, services = _get(path, claims, api)

        _assert(response, 502, MEMBERSHIP_LOOKUP_FAILED)
        services.provisioner.provision.assert_not_awaited()
        messages = [r.getMessage() for r in caplog.records if r.name == "app.services.keycloak_directory"]
        if status is not None:
            assert any(str(status) in message for message in messages), messages
        logged = "\n".join(
            [r.getMessage() for r in caplog.records]
            + [str(r.exc_info[1]) for r in caplog.records if r.exc_info is not None]
        )
        assert DIRECTORY_CLIENT_SECRET not in logged

    def test_a_list_that_does_not_answer_within_the_limit_is_502(self, monkeypatch):
        async def never(claims):
            await asyncio.sleep(2)
            return [TenantMembership(ACME, "Acme")]

        monkeypatch.setattr(rbac, "MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS", 0.01)
        provider = replace(
            _keycloak(AdminApi([organization(ACME)])),
            sole_membership_lookup=CountedMemberships(never),
            tenant_memberships=never,
        )
        limiter.reset()
        client = TestClient(_app([descope_config(DESCOPE_PROJECT), provider], _Services(CANONICAL)))

        response = client.get(IDENTITY, headers=_bearer(_member()))

        _assert(response, 502, MEMBERSHIP_LOOKUP_FAILED)

    @pytest.mark.parametrize("answer", [None, "acme", [ACME], {TenantMembership(ACME, "Acme")}], ids=repr)
    def test_a_list_of_the_wrong_shape_is_502(self, answer):
        async def wrong(claims):
            return answer

        provider = replace(
            _keycloak(AdminApi([organization(ACME)])),
            sole_membership_lookup=CountedMemberships(wrong),
            tenant_memberships=wrong,
        )
        limiter.reset()
        client = TestClient(_app([descope_config(DESCOPE_PROJECT), provider], _Services(CANONICAL)))

        response = client.get(IDENTITY, headers=_bearer(_member()))

        _assert(response, 502, MEMBERSHIP_LOOKUP_FAILED)


class TestWhatTheTokenAloneDecidesComesFirst:
    """Done when 8: a token that fails a token-only check gets its answer, and no lookup is made."""

    @pytest.mark.parametrize(
        ("claims", "detail"),
        [
            (_member(organization=None), NO_TENANT_CONTEXT),
            (_member(organization=[ACME, GLOBEX]), NO_TENANT_CONTEXT),
            (_member(resource_access={}), NO_TENANT_CONTEXT),
        ],
        ids=["no-organization", "two-organizations", "unreadable-roles"],
    )
    def test_a_token_that_names_no_tenant_is_403_with_the_directory_down(self, claims, detail):
        api = AdminApi(transport_error=True)

        response, services = _get(IDENTITY, claims, api)

        _assert(response, 403, detail)
        assert api.requests == []
        services.resolver.resolve.assert_not_awaited()
        services.provisioner.provision.assert_not_awaited()

    def test_an_unbound_token_is_401_and_nothing_is_asked(self):
        api = AdminApi(transport_error=True)

        response, _ = _get(IDENTITY, _member(azp="account"), api)

        assert response.status_code == 401
        assert api.requests == []


class TestAnOrganizationBoundToken:
    """Done when 10, against the lookup the provider wires."""

    def test_the_single_organization_tenant_admin_is_allowed(self):
        api = AdminApi([organization(ACME, name="Acme", attributes={TENANT_ADMIN_MARKER: [SUBJECT]})])

        check, _ = _get(SETTINGS_MANAGE_ROUTE, _tenant_admin(), api)
        identity, _ = _get(IDENTITY, _tenant_admin(), api)

        assert check.status_code == 200, check.text
        assert check.json() == {"tenant_id": ACME, "permission": SETTINGS_MANAGE}
        assert identity.status_code == 200, identity.text
        assert identity.json()["token_tenant"] == {"tenant_id": ACME, "permissions": [SETTINGS_MANAGE]}

    def test_the_two_organization_tenant_admin_is_refused_on_both(self):
        api = _acme_and_globex(**{TENANT_ADMIN_MARKER: [SUBJECT]})

        check, _ = _get(SETTINGS_MANAGE_ROUTE, _tenant_admin(), api)
        identity, services = _get(IDENTITY, _tenant_admin(), api)

        _assert(check, 403, SOLE_MEMBERSHIP_NOT_CONFIRMED)
        _assert(identity, 403, SOLE_MEMBERSHIP_NOT_CONFIRMED)
        services.provisioner.provision.assert_not_awaited()

    def test_a_tenant_admin_the_organization_does_not_mark_is_refused(self):
        """Admin of A, then a member of B alone: the realm-wide roles are not B's grant."""
        api = AdminApi([organization(GLOBEX, name="Globex")])

        check, _ = _get(SETTINGS_MANAGE_ROUTE, _tenant_admin(organization=[GLOBEX]), api)
        identity, _ = _get(IDENTITY, _tenant_admin(organization=[GLOBEX]), api)

        _assert(check, 403, SOLE_MEMBERSHIP_NOT_CONFIRMED)
        _assert(identity, 403, SOLE_MEMBERSHIP_NOT_CONFIRMED)


class TestNoRequestInputReachesTheMemberId:
    """Done when 6: the path, query, headers and body name other users; the lookup still uses ``sub``."""

    OTHER = "b0b00000-0000-4000-8000-000000000002"

    @pytest.mark.parametrize("claims", [_member(), _tenant_admin()], ids=["member", "tenant-admin"])
    def test_every_lookup_names_the_tokens_sub(self, claims):
        api = AdminApi([organization(ACME, attributes={TENANT_ADMIN_MARKER: [SUBJECT]})])
        limiter.reset()
        client = TestClient(_app([descope_config(DESCOPE_PROJECT), _keycloak(api)], _Services(CANONICAL)))

        for path in (IDENTITY, f"{IDENTITY}/{self.OTHER}", SETTINGS_MANAGE_ROUTE):
            client.request(
                "GET",
                path,
                params={"sub": self.OTHER, "member": self.OTHER, "user_id": self.OTHER},
                headers={
                    **_bearer(claims),
                    "X-User-ID": self.OTHER,
                    "X-Tenant-ID": GLOBEX,
                    "X-Forwarded-User": self.OTHER,
                },
                json={"sub": self.OTHER, "member_id": self.OTHER},
            )

        member_ids = {request.url.raw_path.decode().split("/")[-2] for request in api.lookups()}
        assert api.lookups()
        assert member_ids == {SUBJECT}


class TestDescopeAndOryAnswersAreUnchanged:
    """Done when 5: the same bytes as before, and no directory is asked."""

    @pytest.mark.parametrize(
        ("provider", "claims"),
        [
            (
                ory_config(ORY_ISSUER, ORY_AUDIENCE),
                {"iss": ORY_ISSUER, "aud": ORY_AUDIENCE, "sub": "ory-user", "organization": [ACME]},
            ),
            (
                descope_config(DESCOPE_PROJECT),
                {"iss": DESCOPE_PROJECT, "sub": "U1", "drn": "DS", "dct": ACME, "tenants": {ACME: {"roles": []}}},
            ),
        ],
        ids=["ory", "descope"],
    )
    def test_the_response_is_the_canonical_identity_byte_for_byte(self, provider, claims):
        api = AdminApi([organization(ACME)])
        providers = list({p.name: p for p in [descope_config(DESCOPE_PROJECT), provider, _keycloak(api)]}.values())
        limiter.reset()
        client = TestClient(_app(providers, _Services(CANONICAL)))

        response = client.get(IDENTITY, headers=_bearer({**claims, "exp": int(time.time()) + SECONDS_OF_VALIDITY}))

        assert response.status_code == 200, response.text
        assert response.content == JSONResponse(CANONICAL).body
        assert api.requests == []
