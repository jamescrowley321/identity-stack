"""A Keycloak principal on the shipped routes: bound at authentication, decided from its token.

Token claims start from ``keycloak/fixtures/access-token.json``, the decoded
access token the local realm issued. Each test changes the claim it is about.
The app is every shipped route behind a shipped token middleware, plus a few
routes of the test's own where the application has none that fits.
"""

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from expression import Error
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies.identity import get_identity_provisioning_service, get_identity_resolution_service
from app.dependencies.rbac import (
    INSUFFICIENT_PERMISSIONS,
    MEMBERSHIP_LOOKUP_FAILED,
    get_rbac_session,
    require_permission,
    require_role,
)
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.dependencies.tenant_access import (
    NO_TENANT_CONTEXT,
    SELECT_A_TENANT_FIRST,
    SOLE_MEMBERSHIP_NOT_CONFIRMED,
    resolve_tenant_access,
)
from app.errors.identity import NotFound
from app.main import app as shipped_app
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.claims import GatewayClaimsMiddleware
from app.middleware.providers import ProviderTokenConfig, TenantClaims, descope_config, keycloak_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.models.database import get_async_session
from tests.helpers.keycloak_model import (
    MEMBER_TYPE_CLIENT,
    TENANT_ADMIN_APP_CLIENT,
    TENANT_ADMIN_TYPE_CLIENT,
    model_settings,
)

FIXTURE = Path(__file__).resolve().parents[3] / "keycloak" / "fixtures" / "access-token.json"
FIXTURE_CLAIMS = json.loads(FIXTURE.read_text())["claims"]
ISSUER = FIXTURE_CLAIMS["iss"]
SUBJECT = FIXTURE_CLAIMS["sub"]

ACME = "acme"
GLOBEX = "globex"
DOCUMENTS_READ = "documents:read"
DOCUMENTS_WRITE = "documents:write"
SETTINGS_MANAGE = "settings:manage"
MEMBERS_INVITE = "members:invite"

DOCUMENTS_READ_ROUTE = "/api/access-checks/documents-read"
SETTINGS_MANAGE_ROUTE = "/api/access-checks/settings-manage"
OWN_WRITE_ROUTE = "/own/documents-write"
OWN_OWNER_ROUTE = "/own/owner"
OWN_TENANT_CLAIMS_ROUTE = "/own/tenant-claims"
OWN_STATE_ROUTE = "/own/state"

UNIFORM_401 = {"detail": "Invalid or expired token"}
STANDALONE = "standalone"
GATEWAY = "gateway"
MIDDLEWARES = {STANDALONE: TokenValidationMiddleware, GATEWAY: GatewayClaimsMiddleware}
SECONDS_OF_VALIDITY = 300


def _provider(lookup=None) -> ProviderTokenConfig:
    return keycloak_config(
        ISSUER,
        frozenset({MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT}),
        require_https=False,
        organization_lookup=lookup,
        **model_settings(MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT),
    )


def _member(**changes) -> dict:
    """The fixture token, not expired, with ``changes`` applied; ``None`` removes a claim."""
    claims = {**FIXTURE_CLAIMS, "exp": int(time.time()) + SECONDS_OF_VALIDITY, **changes}
    return {name: value for name, value in claims.items() if value is not None}


def _tenant_admin(**changes) -> dict:
    """The same user's token through ``tenant-admin-app``."""
    return _member(
        **{
            "aud": TENANT_ADMIN_TYPE_CLIENT,
            "azp": TENANT_ADMIN_APP_CLIENT,
            "resource_access": {TENANT_ADMIN_TYPE_CLIENT: {"roles": [SETTINGS_MANAGE, MEMBERS_INVITE]}},
            **changes,
        }
    )


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _bearer(claims: dict) -> dict[str, str]:
    """An unsigned JWT: gateway mode reads the payload, standalone mode has ``validate_token`` patched."""
    return {"Authorization": f"Bearer {_segment({'alg': 'RS256'})}.{_segment(claims)}.sig"}


class _Lookup:
    """A test double of the membership lookup: a canned answer or error, every call recorded."""

    def __init__(self, answer: object = None, *, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict] = []

    async def __call__(self, claims: dict):
        self.calls.append(claims)
        if self.error is not None:
            raise self.error
        return self.answer


def _app(mode: str, provider: ProviderTokenConfig) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.state.descope_client = AsyncMock()
    app.state.cache_publisher = MagicMock()
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(shipped_app.router)

    @app.get(OWN_WRITE_ROUTE, dependencies=[Depends(require_permission(DOCUMENTS_WRITE))])
    async def documents_write(tenant_id: str = Depends(get_tenant_id)):
        return {"tenant_id": tenant_id}

    @app.get(OWN_OWNER_ROUTE)
    async def owner(roles: list[str] = Depends(require_role("owner"))):
        return {"roles": roles}

    @app.get(OWN_TENANT_CLAIMS_ROUTE)
    async def tenant_claims(tenants: dict = Depends(get_tenant_claims)):
        return {"tenants": tenants}

    @app.get(OWN_STATE_ROUTE)
    async def state(request: Request):
        return {"tenant_id": request.state.tenant_id}

    async def _session():
        yield MagicMock()

    # ``GET /api/identity`` finds no canonical identity: this file is about what comes before it.
    resolver = AsyncMock()
    resolver.resolve.return_value = Error(NotFound("no canonical identity"))

    app.dependency_overrides[get_rbac_session] = _session
    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_identity_resolution_service] = lambda: resolver
    app.dependency_overrides[get_identity_provisioning_service] = lambda: AsyncMock()
    app.add_middleware(MIDDLEWARES[mode], providers=[descope_config("P123"), provider])
    return app


def _get(claims: dict, path: str, *, mode: str = GATEWAY, lookup=None, app: FastAPI | None = None):
    limiter.reset()
    client = TestClient(app or _app(mode, _provider(lookup)))
    if mode == GATEWAY:
        return client.get(path, headers=_bearer(claims))
    with patch("app.middleware.auth.validate_token", new_callable=AsyncMock) as validate:
        validate.return_value = dict(claims)
        return client.get(path, headers=_bearer(claims))


def _assert_403(response, detail: str) -> None:
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
class TestTheTokenBindingHoldsOnEveryRoute:
    """Done when 3: refused at authentication, with the uniform 401, whatever the route depends on."""

    ROUTES = ("/api/identity", "/api/me", DOCUMENTS_READ_ROUTE)

    @pytest.mark.parametrize("path", ROUTES)
    def test_the_fixture_token_authenticates(self, mode, path):
        """Control: the routes below refuse for the binding and for nothing else."""
        assert _get(_member(), path, mode=mode).status_code != 401

    @pytest.mark.parametrize("path", ROUTES)
    def test_several_audiences_are_refused_even_when_each_is_a_type_client(self, mode, path):
        response = _get(_member(aud=[MEMBER_TYPE_CLIENT, TENANT_ADMIN_TYPE_CLIENT]), path, mode=mode)

        assert response.status_code == 401
        assert response.json() == UNIFORM_401

    @pytest.mark.parametrize("path", ROUTES)
    def test_an_audience_beside_one_that_is_not_a_type_client_is_refused(self, mode, path):
        response = _get(_member(aud=[MEMBER_TYPE_CLIENT, "account"]), path, mode=mode)

        assert response.status_code == 401
        assert response.json() == UNIFORM_401

    @pytest.mark.parametrize("path", ROUTES)
    @pytest.mark.parametrize("azp", [None, "", 7, ["member-app"], TENANT_ADMIN_APP_CLIENT, "account"], ids=repr)
    def test_an_azp_that_is_not_the_app_client_of_the_audience_is_refused(self, mode, path, azp):
        response = _get(_member(azp=azp), path, mode=mode)

        assert response.status_code == 401
        assert response.json() == UNIFORM_401

    def test_a_one_element_audience_list_is_one_audience(self, mode):
        response = _get(_member(aud=[MEMBER_TYPE_CLIENT]), DOCUMENTS_READ_ROUTE, mode=mode)

        assert response.status_code == 200
        assert response.json() == {"tenant_id": ACME, "permission": DOCUMENTS_READ}

    def test_the_cause_is_logged_and_not_returned(self, mode, caplog):
        logger = "app.middleware.auth" if mode == STANDALONE else "app.middleware.claims"

        with caplog.at_level("WARNING", logger=logger):
            response = _get(_member(azp="some-other-client"), "/api/me", mode=mode)

        assert response.json() == UNIFORM_401
        messages = [record.getMessage() for record in caplog.records if record.name == logger]
        assert any("azp is not the app client configured for the token's aud" in message for message in messages)
        assert not any("some-other-client" in message for message in messages)


class TestTheOrganizationIsTheTenant:
    @pytest.mark.parametrize("alias", [ACME, GLOBEX])
    def test_each_token_acts_in_the_organization_it_names(self, alias):
        response = _get(_member(organization=[alias]), DOCUMENTS_READ_ROUTE)

        assert response.status_code == 200
        assert response.json() == {"tenant_id": alias, "permission": DOCUMENTS_READ}

    def test_the_tenant_is_the_same_in_standalone_mode(self):
        response = _get(_member(), DOCUMENTS_READ_ROUTE, mode=STANDALONE)

        assert response.json() == {"tenant_id": ACME, "permission": DOCUMENTS_READ}

    def test_a_map_of_one_key_names_the_tenant_and_its_value_is_not_read(self):
        response = _get(_member(organization={GLOBEX: {"id": "42", "roles": ["owner"]}}), DOCUMENTS_READ_ROUTE)

        assert response.json() == {"tenant_id": GLOBEX, "permission": DOCUMENTS_READ}

    @pytest.mark.parametrize(
        "claim",
        [None, [], [""], [ACME, GLOBEX], [7], {}, {"": {}}, {ACME: {}, GLOBEX: {}}, ACME, 7, True],
        ids=repr,
    )
    def test_any_other_organization_claim_is_refused(self, claim):
        """A missing claim is what Keycloak gives a user outside the organization that was asked for."""
        _assert_403(_get(_member(organization=claim), DOCUMENTS_READ_ROUTE), NO_TENANT_CONTEXT)

    def test_the_middleware_records_the_alias_as_the_requests_tenant(self):
        assert _get(_member(), OWN_STATE_ROUTE).json() == {"tenant_id": ACME}

    def test_a_token_that_names_no_organization_still_authenticates(self):
        """403 from a dependency, not 401: the route below has none."""
        response = _get(_member(organization=None), OWN_STATE_ROUTE)

        assert response.status_code == 200
        assert response.json() == {"tenant_id": None}


class TestClientRolesAreThePermissions:
    @pytest.mark.parametrize(
        "resource_access",
        [
            None,
            {},
            {TENANT_ADMIN_TYPE_CLIENT: {"roles": [DOCUMENTS_READ]}},
            {MEMBER_TYPE_CLIENT: {}},
            {MEMBER_TYPE_CLIENT: {"roles": DOCUMENTS_READ}},
            {MEMBER_TYPE_CLIENT: {"roles": [DOCUMENTS_READ, 7]}},
            {MEMBER_TYPE_CLIENT: [DOCUMENTS_READ]},
        ],
        ids=repr,
    )
    def test_roles_that_are_missing_or_of_the_wrong_type_grant_nothing(self, resource_access):
        response = _get(_member(resource_access=resource_access), DOCUMENTS_READ_ROUTE)

        _assert_403(response, INSUFFICIENT_PERMISSIONS)

    def test_a_member_without_documents_read_is_refused(self):
        claims = _member(resource_access={MEMBER_TYPE_CLIENT: {"roles": [DOCUMENTS_WRITE]}})

        _assert_403(_get(claims, DOCUMENTS_READ_ROUTE), INSUFFICIENT_PERMISSIONS)

    def test_a_member_token_carrying_documents_write_is_refused_on_a_route_needing_it(self):
        """A portable type grants only what is on its portable list, and a write permission never is."""
        claims = _member(resource_access={MEMBER_TYPE_CLIENT: {"roles": [DOCUMENTS_READ, DOCUMENTS_WRITE]}})

        _assert_403(_get(claims, OWN_WRITE_ROUTE), INSUFFICIENT_PERMISSIONS)
        # Control: the same token does hold what is on the list.
        assert _get(claims, DOCUMENTS_READ_ROUTE).status_code == 200

    def test_a_member_token_does_not_hold_a_tenant_admin_permission(self):
        _assert_403(_get(_member(), SETTINGS_MANAGE_ROUTE), INSUFFICIENT_PERMISSIONS)

    def test_realm_roles_are_not_permissions(self):
        claims = _member(
            realm_access={"roles": [DOCUMENTS_READ, DOCUMENTS_WRITE]},
            resource_access={MEMBER_TYPE_CLIENT: {"roles": []}},
        )

        _assert_403(_get(claims, DOCUMENTS_READ_ROUTE), INSUFFICIENT_PERMISSIONS)

    def test_a_role_gated_route_refuses_a_keycloak_principal(self):
        """The token has permissions and a user type, no role."""
        claims = _member(resource_access={MEMBER_TYPE_CLIENT: {"roles": [DOCUMENTS_READ, "owner"]}})

        _assert_403(_get(claims, OWN_OWNER_ROUTE), INSUFFICIENT_PERMISSIONS)


class TestAnotherProvidersClaimsOnAKeycloakToken:
    """Done when 6: ``dct`` and ``tenants`` naming another tenant with an ``owner`` role gain nothing."""

    FORGED = {
        "dct": "other-tenant",
        "tenants": {
            "other-tenant": {"roles": ["owner"], "permissions": [DOCUMENTS_WRITE, SETTINGS_MANAGE]},
            ACME: {"roles": ["owner"], "permissions": [DOCUMENTS_WRITE, SETTINGS_MANAGE]},
        },
        "roles": ["owner"],
    }

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_the_tenant_is_the_organization_alias(self, mode):
        response = _get(_member(**self.FORGED), DOCUMENTS_READ_ROUTE, mode=mode)

        assert response.json() == {"tenant_id": ACME, "permission": DOCUMENTS_READ}

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_the_requests_tenant_is_not_the_forged_dct(self, mode):
        assert _get(_member(**self.FORGED), OWN_STATE_ROUTE, mode=mode).json() == {"tenant_id": ACME}

    def test_the_tenants_claim_is_not_returned(self):
        assert _get(_member(**self.FORGED), OWN_TENANT_CLAIMS_ROUTE).json() == {"tenants": {}}

    def test_the_owner_role_is_not_held(self):
        _assert_403(_get(_member(**self.FORGED), OWN_OWNER_ROUTE), INSUFFICIENT_PERMISSIONS)

    def test_the_forged_permissions_are_not_held(self):
        _assert_403(_get(_member(**self.FORGED), OWN_WRITE_ROUTE), INSUFFICIENT_PERMISSIONS)
        _assert_403(_get(_member(**self.FORGED), SETTINGS_MANAGE_ROUTE), INSUFFICIENT_PERMISSIONS)

    def test_a_forged_dct_does_not_stand_in_for_a_missing_organization(self):
        claims = _member(organization=None, **self.FORGED)

        _assert_403(_get(claims, DOCUMENTS_READ_ROUTE), NO_TENANT_CONTEXT)
        assert _get(claims, OWN_STATE_ROUTE).json() == {"tenant_id": None}

    def test_the_membership_list_is_empty_and_descope_is_not_asked(self):
        app = _app(GATEWAY, _provider())

        response = _get(_member(**self.FORGED), "/api/tenants", app=app)

        assert response.status_code == 200
        assert response.json() == {"tenants": []}
        assert app.state.descope_client.mock_calls == []

    def test_a_descope_principal_still_gets_its_tenants_claim(self):
        """Control: only a provider with its own extractor loses the claim."""
        tenants = {"t1": {"roles": ["owner"], "permissions": []}}
        claims = {
            "iss": "https://api.descope.com/P123",
            "sub": "descope-user",
            "exp": int(time.time()) + SECONDS_OF_VALIDITY,
            "dct": "t1",
            "tenants": tenants,
        }

        assert _get(claims, OWN_TENANT_CLAIMS_ROUTE).json() == {"tenants": tenants}
        assert _get(claims, OWN_OWNER_ROUTE).json() == {"roles": ["owner"]}


class TestAnOrganizationBoundTokenWithNoLookupWired:
    """Done when 7: the state this backend ships in. Every ``tenant-admin`` token is refused."""

    @pytest.mark.parametrize("alias", [ACME, GLOBEX])
    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_the_settings_manage_route_refuses_it(self, mode, alias):
        response = _get(_tenant_admin(organization=[alias]), SETTINGS_MANAGE_ROUTE, mode=mode)

        _assert_403(response, SOLE_MEMBERSHIP_NOT_CONFIRMED)

    def test_its_tenant_is_not_believed_either(self):
        app = _app(GATEWAY, _provider())

        @app.get("/own/tenant")
        async def tenant(tenant_id: str = Depends(get_tenant_id)):
            return {"tenant_id": tenant_id}

        _assert_403(_get(_tenant_admin(), "/own/tenant", app=app), SOLE_MEMBERSHIP_NOT_CONFIRMED)

    def test_the_refusal_does_not_say_to_select_a_tenant(self):
        assert SOLE_MEMBERSHIP_NOT_CONFIRMED not in (NO_TENANT_CONTEXT, SELECT_A_TENANT_FIRST)

    def test_get_tenant_id_called_with_the_request_alone_refuses(self):
        """Nothing confirmed the token, so the resolver grants nothing by itself."""
        request = SimpleNamespace(
            state=SimpleNamespace(claims=_tenant_admin(), provider=_provider()),
            headers=SimpleNamespace(getlist=lambda _name: []),
        )

        with pytest.raises(HTTPException) as refused:
            get_tenant_id(request)

        assert refused.value.status_code == 403
        assert refused.value.detail == SOLE_MEMBERSHIP_NOT_CONFIRMED

    def test_a_portable_token_needs_no_lookup(self):
        assert _get(_member(), DOCUMENTS_READ_ROUTE).status_code == 200


class TestAnOrganizationBoundTokenAgainstALookupDouble:
    """Done when 8. ``_Lookup`` stands in for the directory lookup, which is not part of this backend yet."""

    @pytest.mark.parametrize("alias", [ACME, GLOBEX])
    def test_a_tenant_admin_in_two_organizations_is_refused_in_both(self, alias):
        lookup = _Lookup([ACME, GLOBEX])

        response = _get(_tenant_admin(organization=[alias]), SETTINGS_MANAGE_ROUTE, lookup=lookup)

        _assert_403(response, SOLE_MEMBERSHIP_NOT_CONFIRMED)

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_a_tenant_admin_in_the_tokens_organization_alone_is_allowed(self, mode):
        lookup = _Lookup([ACME])

        response = _get(_tenant_admin(), SETTINGS_MANAGE_ROUTE, mode=mode, lookup=lookup)

        assert response.status_code == 200
        assert response.json() == {"tenant_id": ACME, "permission": SETTINGS_MANAGE}

    def test_the_lookup_is_asked_once_with_the_verified_claims(self):
        """The route depends on the permission and on the tenant: one request, one lookup."""
        lookup = _Lookup([ACME])
        claims = _tenant_admin()

        _get(claims, SETTINGS_MANAGE_ROUTE, lookup=lookup)

        assert lookup.calls == [claims]

    @pytest.mark.parametrize("answer", [[], [GLOBEX], (), frozenset({GLOBEX}), [ACME, ACME, GLOBEX]], ids=repr)
    def test_any_answer_but_the_tokens_organization_alone_is_refused(self, answer):
        response = _get(_tenant_admin(), SETTINGS_MANAGE_ROUTE, lookup=_Lookup(answer))

        _assert_403(response, SOLE_MEMBERSHIP_NOT_CONFIRMED)

    @pytest.mark.parametrize("mode", [STANDALONE, GATEWAY])
    def test_a_lookup_that_raises_is_502_and_no_grant(self, mode):
        lookup = _Lookup(error=RuntimeError("directory unreachable: secret-detail"))

        response = _get(_tenant_admin(), SETTINGS_MANAGE_ROUTE, mode=mode, lookup=lookup)

        assert response.status_code == 502
        assert response.json() == {"detail": MEMBERSHIP_LOOKUP_FAILED}

    @pytest.mark.parametrize("answer", [None, ACME, {ACME: True}, [ACME, 7], 7], ids=repr)
    def test_an_answer_that_is_not_a_collection_of_aliases_is_502_and_no_grant(self, answer):
        """A bare string would otherwise be read letter by letter."""
        response = _get(_tenant_admin(), SETTINGS_MANAGE_ROUTE, lookup=_Lookup(answer))

        assert response.status_code == 502
        assert response.json() == {"detail": MEMBERSHIP_LOOKUP_FAILED}

    def test_a_permission_the_token_lacks_is_refused_without_asking(self):
        """What the token alone decides comes first: a directory that is down does not turn 403 into 502."""
        lookup = _Lookup(error=RuntimeError("directory unreachable"))
        claims = _tenant_admin(resource_access={TENANT_ADMIN_TYPE_CLIENT: {"roles": [MEMBERS_INVITE]}})

        _assert_403(_get(claims, SETTINGS_MANAGE_ROUTE, lookup=lookup), INSUFFICIENT_PERMISSIONS)
        assert lookup.calls == []

    def test_a_token_that_names_no_organization_is_refused_without_asking(self):
        lookup = _Lookup(error=RuntimeError("directory unreachable"))

        _assert_403(_get(_tenant_admin(organization=None), SETTINGS_MANAGE_ROUTE, lookup=lookup), NO_TENANT_CONTEXT)
        assert lookup.calls == []

    def test_an_unbound_token_is_refused_at_authentication_without_asking(self):
        lookup = _Lookup([ACME])

        response = _get(_tenant_admin(azp="member-app"), SETTINGS_MANAGE_ROUTE, lookup=lookup)

        assert response.status_code == 401
        assert lookup.calls == []

    def test_a_portable_token_is_never_looked_up(self):
        lookup = _Lookup(error=RuntimeError("directory unreachable"))

        assert _get(_member(), DOCUMENTS_READ_ROUTE, lookup=lookup).status_code == 200
        assert lookup.calls == []

    def test_a_role_gate_refuses_a_confirmed_tenant_admin_too(self):
        lookup = _Lookup([ACME])

        _assert_403(_get(_tenant_admin(), OWN_OWNER_ROUTE, lookup=lookup), INSUFFICIENT_PERMISSIONS)
        assert lookup.calls == []

    def test_the_failure_is_logged_with_the_caller_and_the_refusal_names_no_tenant(self, caplog):
        lookup = _Lookup([ACME, GLOBEX])

        with caplog.at_level("WARNING", logger="app.dependencies.rbac"):
            _get(_tenant_admin(), SETTINGS_MANAGE_ROUTE, lookup=lookup)

        messages = [record.getMessage() for record in caplog.records if record.name == "app.dependencies.rbac"]
        assert messages == [
            f"membership not confirmed: provider=Keycloak sub={SUBJECT!r} belongs to 2 tenant(s), "
            "not to the token's alone"
        ]


class TestTheResolverHoldsAnyProviderToTheConfirmation:
    """The rule is the resolver's, not Keycloak's: whichever extractor asks for a confirmation needs one."""

    @staticmethod
    def _request(flag: object, **state: object) -> SimpleNamespace:
        def extractor(claims: dict) -> TenantClaims:
            return TenantClaims(tenant_id=claims["org"], roles=["admin"], permissions=[], sole_membership_required=flag)

        provider = ProviderTokenConfig(
            name="Other",
            accepted_issuers=frozenset({"https://other.example.test"}),
            disco_address="https://other.example.test/.well-known/openid-configuration",
            tenant_claims=extractor,
        )
        return SimpleNamespace(state=SimpleNamespace(provider=provider, **state))

    CLAIMS = {"sub": "other-user", "org": "org-1"}

    def test_claims_that_need_no_confirmation_are_believed(self):
        access = resolve_tenant_access(self._request(False), self.CLAIMS, None)

        assert (access.tenant_id, access.roles) == ("org-1", ["admin"])

    def test_claims_that_need_one_are_refused_without_it(self):
        with pytest.raises(HTTPException) as refused:
            resolve_tenant_access(self._request(True), self.CLAIMS, None)

        assert refused.value.status_code == 403
        assert refused.value.detail == SOLE_MEMBERSHIP_NOT_CONFIRMED

    def test_a_confirmation_of_another_tenant_does_not_count(self):
        with pytest.raises(HTTPException) as refused:
            resolve_tenant_access(self._request(True), self.CLAIMS, None, confirmed_tenant="org-2")

        assert refused.value.status_code == 403
        assert refused.value.detail == SOLE_MEMBERSHIP_NOT_CONFIRMED

    def test_a_confirmation_of_the_tokens_tenant_is_believed(self):
        access = resolve_tenant_access(self._request(True), self.CLAIMS, None, confirmed_tenant="org-1")

        assert (access.tenant_id, access.roles) == ("org-1", ["admin"])

    @pytest.mark.parametrize("flag", [None, "no", "false", 0, 1, []], ids=repr)
    def test_a_flag_that_is_not_a_boolean_is_an_answer_that_cannot_be_used(self, flag):
        """It could be read either way, so nothing is granted, confirmed or not."""
        with pytest.raises(HTTPException) as refused:
            resolve_tenant_access(self._request(flag), self.CLAIMS, None, confirmed_tenant="org-1")

        assert refused.value.status_code == 403
        assert refused.value.detail == NO_TENANT_CONTEXT
