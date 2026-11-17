"""First login of a Zitadel user: the organization claim, UserInfo, and what ``GET /api/identity`` does with them.

Tokens are the recorded ones (``tests/fixtures/zitadel/``) re-stamped onto a
loopback provider (``tests/helpers/local_op.py``), which also answers UserInfo
with the recorded UserInfo response. The middleware validates each token and
the backend fetches UserInfo through the real library. The canonical services
are doubles: what provisioning writes is ``tests/integration/test_zitadel_provisioning.py``.
"""

import ast
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from expression import Error, Ok
from fastapi import FastAPI
from fastapi.testclient import TestClient
from py_identity_model import to_principal
from slowapi.errors import RateLimitExceeded

from app.dependencies.identity import get_identity_provisioning_service, get_identity_resolution_service
from app.errors.identity import EmailLinkRefused, Forbidden, NotFound, ProviderError, ValidationError
from app.main import app as shipped_app
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import ProviderTokenConfig, descope_config, zitadel_config
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.middleware.zitadel import ORGANIZATION_CLAIM, organization_id
from app.routers import protected
from app.services.first_login_profile import FirstLoginProfile, userinfo_profile
from tests.helpers.local_op import RS256, LocalProvider, local_provider

APP_DIR = Path(__file__).resolve().parents[2] / "app"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "zitadel"
IDENTITY = "/api/identity"
PROJECT_ID = "399781183140937731"
SPA_CLIENT_ID = "399781187016540163"


def _recorded(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


ACCESS = _recorded("human-access-token-project.json")["claims"]
USERINFO = _recorded("human-userinfo-project.json")
SUB = ACCESS["sub"]
ORGANIZATION = ACCESS[ORGANIZATION_CLAIM]
ORGANIZATION_TENANT = uuid.UUID("5b0f6a52-3c9e-4f0e-9d38-0d1f1c2b3a40")
CANONICAL = {"user": {"id": "c0de", "status": "active"}, "roles": [], "tenant_memberships": [], "linked_idps": []}


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


@pytest.fixture
def userinfo(op):
    """UserInfo answers with the recorded response; each test may change it."""
    op.serve_userinfo(dict(USERINFO))
    yield op
    op.serve_userinfo(None)
    op.restore(op.userinfo_path)


def _zitadel(op: LocalProvider) -> ProviderTokenConfig:
    return zitadel_config(op.issuer, PROJECT_ID, SPA_CLIENT_ID, require_https=False)


def _token(op: LocalProvider, name: str = "human-access-token-project.json", **changes) -> str:
    claims = {k: v for k, v in _recorded(name)["claims"].items() if k not in {"iat", "exp", "nbf"}}
    return op.token(RS256, {**claims, "iss": op.issuer, **changes})


# --- the organization claim is read in one function --------------------------


def _string_constants(tree: ast.AST) -> list[ast.Constant]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


class TestTheOrganizationClaim:
    def test_its_name_is_written_once_in_the_application(self):
        found = [
            path.relative_to(APP_DIR).as_posix()
            for path in sorted(APP_DIR.rglob("*.py"))
            for node in _string_constants(ast.parse(path.read_text()))
            if node.value == ORGANIZATION_CLAIM
        ]

        assert found == ["middleware/zitadel.py"]

    def test_it_is_read_by_organization_id_alone(self):
        tree = ast.parse((APP_DIR / "middleware" / "zitadel.py").read_text())
        readers = {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and node.id == "ORGANIZATION_CLAIM"
        }

        assert readers == {"organization_id"}

    def test_only_first_login_calls_its_reader(self):
        """``organization_id`` is the provider's ``first_login_organization``, which only provisioning calls."""
        callers = {
            path.relative_to(APP_DIR).as_posix()
            for path in sorted(APP_DIR.rglob("*.py"))
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Attribute) and node.attr == "first_login_organization"
        }

        assert callers == {"routers/protected.py"}

    def test_it_names_the_recorded_organization(self):
        assert organization_id(ACCESS) == ORGANIZATION

    @pytest.mark.parametrize("value", [None, "", 42, ["399781146147241987"], {"id": "399781146147241987"}])
    def test_anything_but_a_non_empty_string_names_no_organization(self, value):
        claims = {**ACCESS, ORGANIZATION_CLAIM: value}

        assert organization_id(claims) is None

    def test_a_token_requested_without_the_scope_names_none(self):
        assert organization_id(_recorded("human-access-token-openid.json")["claims"]) is None


# --- UserInfo -----------------------------------------------------------------


class TestUserinfoProfile:
    async def test_it_is_the_recorded_userinfo_response(self, op, userinfo):
        result = await userinfo_profile(_zitadel(op), "the-access-token", SUB)

        assert result == Ok(
            FirstLoginProfile(
                email=USERINFO["email"],
                email_verified=True,
                given_name=USERINFO["given_name"],
                family_name=USERINFO["family_name"],
            )
        )

    async def test_it_presents_the_access_token(self, op, userinfo):
        await userinfo_profile(_zitadel(op), "the-access-token", SUB)

        assert op.userinfo_authorizations == ["Bearer the-access-token"]

    async def test_a_different_sub_is_refused(self, op, userinfo):
        op.serve_userinfo({**USERINFO, "sub": "399781146147897348"})

        result = await userinfo_profile(_zitadel(op), "the-access-token", SUB)

        assert result.is_error()
        assert type(result.error) is Forbidden

    async def test_a_missing_sub_is_refused(self, op, userinfo):
        op.serve_userinfo({name: value for name, value in USERINFO.items() if name != "sub"})

        result = await userinfo_profile(_zitadel(op), "the-access-token", SUB)

        assert type(result.error) is Forbidden

    @pytest.mark.parametrize("verified", ["true", 1, None])
    async def test_email_verified_is_true_only_when_it_is_the_boolean(self, op, userinfo, verified):
        op.serve_userinfo({**USERINFO, "email_verified": verified})

        result = await userinfo_profile(_zitadel(op), "the-access-token", SUB)

        assert result.ok.email_verified is False

    async def test_a_missing_email_is_empty(self, op, userinfo):
        op.serve_userinfo({name: value for name, value in USERINFO.items() if name != "email"})

        result = await userinfo_profile(_zitadel(op), "the-access-token", SUB)

        assert result.ok.email == ""

    async def test_userinfo_that_fails_is_a_provider_error(self, op, userinfo):
        op.fail(op.userinfo_path, 500)

        result = await userinfo_profile(_zitadel(op), "the-access-token", SUB)

        assert type(result.error) is ProviderError

    async def test_discovery_that_fails_is_a_provider_error(self):
        with local_provider() as down:
            provider = _zitadel(down)
            down.fail(down.discovery_path, 503)

            result = await userinfo_profile(provider, "the-access-token", SUB)

        assert type(result.error) is ProviderError


# --- GET /api/identity --------------------------------------------------------


class _Services:
    def __init__(self, provisioned, linked) -> None:
        self.resolver = AsyncMock()
        self.resolver.resolve.side_effect = [Error(NotFound("no identity")), Ok(CANONICAL)]
        self.provisioner = AsyncMock()
        self.provisioner.provision.return_value = provisioned
        self.provisioner.organization_tenant_id.return_value = linked


def _get(
    op: LocalProvider, token: str, *, provisioned=Ok("c0de"), linked=Ok(ORGANIZATION_TENANT), jit=("ory", "zitadel")
):
    limiter.reset()
    services = _Services(provisioned, linked)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(shipped_app.router)
    app.dependency_overrides[get_identity_resolution_service] = lambda: services.resolver
    app.dependency_overrides[get_identity_provisioning_service] = lambda: services.provisioner
    app.add_middleware(TokenValidationMiddleware, providers=[descope_config("P-first-login"), _zitadel(op)])
    original = protected.JIT_ENABLED_PROVIDERS
    protected.JIT_ENABLED_PROVIDERS = frozenset(jit)
    try:
        response = TestClient(app).get(IDENTITY, headers={"Authorization": f"Bearer {token}"})
    finally:
        protected.JIT_ENABLED_PROVIDERS = original
    return response, services


class TestAFirstLogin:
    def test_is_provisioned_into_the_organization_with_the_userinfo_profile(self, op, userinfo):
        response, services = _get(op, _token(op))

        assert response.status_code == 200, response.text
        assert response.json() == CANONICAL
        services.provisioner.provision.assert_awaited_once_with(
            provider_name="zitadel",
            sub=SUB,
            email=USERINFO["email"],
            email_verified=True,
            given_name=USERINFO["given_name"],
            family_name=USERINFO["family_name"],
            assign_default_role=True,
            organization_id=ORGANIZATION,
        )

    def test_userinfo_is_asked_with_the_callers_token(self, op, userinfo):
        token = _token(op)

        _get(op, token)

        assert op.userinfo_authorizations == [f"Bearer {token}"]

    def test_without_the_organization_claim_is_refused_before_userinfo(self, op, userinfo):
        response, services = _get(op, _token(op, "human-access-token-openid.json"))

        assert response.status_code == 403
        assert response.json() == {"detail": protected.ORGANIZATION_NOT_LINKED}
        services.provisioner.provision.assert_not_awaited()
        assert op.userinfo_authorizations == []

    def test_an_organization_no_tenant_names_is_refused_before_userinfo(self, op, userinfo):
        response, services = _get(op, _token(op), linked=Error(Forbidden("not linked")))

        assert response.status_code == 403
        assert response.json() == {"detail": protected.ORGANIZATION_NOT_LINKED}
        services.provisioner.organization_tenant_id.assert_awaited_once_with(ORGANIZATION)
        services.provisioner.provision.assert_not_awaited()
        assert op.userinfo_authorizations == []

    def test_an_organization_unlinked_by_the_time_of_provisioning_is_refused(self, op, userinfo):
        response, _ = _get(op, _token(op), provisioned=Error(Forbidden("not linked")))

        assert response.status_code == 403
        assert response.json() == {"detail": protected.ORGANIZATION_NOT_LINKED}

    def test_an_email_another_canonical_user_holds_is_refused_without_saying_so(self, op, userinfo):
        response, _ = _get(op, _token(op), provisioned=Error(EmailLinkRefused("held")))

        assert response.status_code == 409
        assert response.json() == {"detail": protected.PROVISIONING_FAILED}

    def test_a_userinfo_sub_that_differs_is_refused_before_provisioning(self, op, userinfo):
        op.serve_userinfo({**USERINFO, "sub": "399781146147897348"})

        response, services = _get(op, _token(op))

        assert response.status_code == 403
        assert response.json() == {"detail": protected.USERINFO_SUBJECT_MISMATCH}
        services.provisioner.provision.assert_not_awaited()

    def test_an_unverified_email_reaches_provisioning_as_unverified_and_is_refused(self, op, userinfo):
        op.serve_userinfo({**USERINFO, "email_verified": False})

        response, services = _get(op, _token(op), provisioned=Error(ValidationError("unverified")))

        assert response.status_code == 400
        assert services.provisioner.provision.await_args.kwargs["email_verified"] is False

    def test_the_email_claimed_in_the_token_is_not_used(self, op, userinfo):
        token = _token(op, email="attacker@example.test", email_verified=True)

        _, services = _get(op, token)

        assert services.provisioner.provision.await_args.kwargs["email"] == USERINFO["email"]

    def test_userinfo_unavailable_is_503(self, op, userinfo):
        """The same answer as every other provider fault in the module: the provider, not the token."""
        op.fail(op.userinfo_path, 500)

        response, services = _get(op, _token(op))

        assert response.status_code == 503
        assert response.json() == {"detail": protected.UNAVAILABLE_DETAIL}
        services.provisioner.provision.assert_not_awaited()

    def test_without_jit_nothing_is_fetched_or_provisioned(self, op, userinfo):
        response, services = _get(op, _token(op), jit=("ory",))

        assert response.status_code == 404
        services.provisioner.provision.assert_not_awaited()
        assert op.userinfo_authorizations == []


class TestTheRecordedProvider:
    """``_first_login_profile`` trusts only the provider the middleware recorded for this principal."""

    def _get(self, op: LocalProvider, recorded) -> tuple:
        limiter.reset()
        services = _Services(Ok("c0de"), Ok(ORGANIZATION_TENANT))
        claims = {**ACCESS, "email": "token@example.test", "email_verified": True}
        app = FastAPI()
        app.include_router(shipped_app.router)
        app.dependency_overrides[get_identity_resolution_service] = lambda: services.resolver
        app.dependency_overrides[get_identity_provisioning_service] = lambda: services.provisioner

        @app.middleware("http")
        async def _inject(request, call_next):
            request.state.claims = claims
            request.state.principal = to_principal(claims, "zitadel")
            request.state.provider = recorded
            return await call_next(request)

        original = protected.JIT_ENABLED_PROVIDERS
        protected.JIT_ENABLED_PROVIDERS = frozenset({"zitadel"})
        try:
            response = TestClient(app).get(IDENTITY, headers={"Authorization": "Bearer t"})
        finally:
            protected.JIT_ENABLED_PROVIDERS = original
        return response, services

    @pytest.mark.parametrize("recorded", ["zitadel", object()], ids=["a-name", "an-object"])
    def test_one_that_is_not_a_provider_config_is_refused(self, op, recorded):
        response, services = self._get(op, recorded)

        assert response.status_code == 500
        assert response.json() == {"detail": protected.PROVIDER_NOT_CONFIGURED}
        services.provisioner.provision.assert_not_awaited()

    def test_another_providers_config_is_refused(self, op):
        response, services = self._get(op, descope_config("P-first-login"))

        assert response.status_code == 500
        assert response.json() == {"detail": protected.PROVIDER_NOT_CONFIGURED}
        services.provisioner.provision.assert_not_awaited()


class TestAKnownUser:
    def test_is_resolved_without_userinfo(self, op, userinfo):
        limiter.reset()
        resolver = AsyncMock()
        resolver.resolve.return_value = Ok(CANONICAL)
        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(shipped_app.router)
        app.dependency_overrides[get_identity_resolution_service] = lambda: resolver
        app.dependency_overrides[get_identity_provisioning_service] = lambda: MagicMock()
        app.add_middleware(TokenValidationMiddleware, providers=[_zitadel(op)])

        response = TestClient(app).get(IDENTITY, headers={"Authorization": f"Bearer {_token(op)}"})

        assert response.status_code == 200
        assert op.userinfo_authorizations == []
