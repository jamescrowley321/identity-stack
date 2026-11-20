"""First login of a Keycloak user: the organization claim, and what ``GET /api/identity`` does with it.

Tokens are the recorded SPA access token (``keycloak/fixtures/access-token.json``)
re-stamped onto a loopback provider (``tests/helpers/local_op.py``). The
middleware validates each token through the real library. The canonical
services are doubles: what provisioning writes is
``tests/integration/test_keycloak_canonical.py``.
"""

import ast
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from expression import Error, Ok
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded

from app.dependencies.identity import get_identity_provisioning_service, get_identity_resolution_service
from app.errors.identity import EmailLinkRefused, Forbidden, NotFound, ValidationError
from app.main import app as shipped_app
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import (
    KEYCLOAK_ORGANIZATION_CLAIM,
    ProviderTokenConfig,
    descope_config,
    keycloak_config,
    keycloak_organization_alias,
)
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import protected
from tests.helpers.keycloak_model import API_AUDIENCE, SPA_CLIENT_ID
from tests.helpers.local_op import RS256, LocalProvider, local_provider

APP_DIR = Path(__file__).resolve().parents[2] / "app"
FIXTURES = Path(__file__).resolve().parents[3] / "keycloak" / "fixtures"
IDENTITY = "/api/identity"
ACCESS = json.loads((FIXTURES / "access-token.json").read_text())["claims"]
SUB = ACCESS["sub"]
ALIAS = "acme"
ORGANIZATION_TENANT = uuid.UUID("5b0f6a52-3c9e-4f0e-9d38-0d1f1c2b3a41")
CANONICAL = {"user": {"id": "c0de", "status": "active"}, "roles": [], "tenant_memberships": [], "linked_idps": []}


@pytest.fixture(scope="module")
def op() -> LocalProvider:
    with local_provider() as provider:
        yield provider


def _keycloak(op: LocalProvider) -> ProviderTokenConfig:
    return keycloak_config(op.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)


def _token(op: LocalProvider, **changes) -> str:
    claims = {k: v for k, v in ACCESS.items() if k not in {"iat", "exp", "nbf"}}
    claims = {**claims, "iss": op.issuer, **changes}
    return op.token(RS256, {k: v for k, v in claims.items() if v is not None})


# --- the organization claim is read in one function --------------------------


def _string_constants(tree: ast.AST) -> list[ast.Constant]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


class TestTheOrganizationClaim:
    def test_its_name_is_written_once_where_requests_are_authorized(self):
        """``organization`` is a common word (the Zitadel admin API sends it too), so only the request path is read."""
        found = [
            path.relative_to(APP_DIR).as_posix()
            for directory in ("middleware", "dependencies", "routers")
            for path in sorted((APP_DIR / directory).rglob("*.py"))
            for node in _string_constants(ast.parse(path.read_text()))
            if node.value == KEYCLOAK_ORGANIZATION_CLAIM
        ]

        assert found == ["middleware/providers.py"]

    def test_it_is_read_by_keycloak_organization_alias_alone(self):
        tree = ast.parse((APP_DIR / "middleware" / "providers.py").read_text())
        readers = {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and node.id == "KEYCLOAK_ORGANIZATION_CLAIM"
        }

        assert readers == {"keycloak_organization_alias"}

    def test_it_names_the_recorded_alias(self):
        assert keycloak_organization_alias(ACCESS) == ALIAS

    def test_a_map_of_one_alias_names_it(self):
        assert keycloak_organization_alias({KEYCLOAK_ORGANIZATION_CLAIM: {ALIAS: {"name": "Acme"}}}) == ALIAS

    @pytest.mark.parametrize(
        "value",
        [None, "", ALIAS, [], [""], [7], ["globex", ALIAS], {}, {"": {}}, {ALIAS: {}, "globex": {}}, 7],
        ids=lambda value: repr(value),
    )
    def test_anything_but_exactly_one_alias_names_no_organization(self, value):
        assert keycloak_organization_alias({**ACCESS, KEYCLOAK_ORGANIZATION_CLAIM: value}) is None

    def test_a_token_without_the_claim_names_none(self):
        claims = {k: v for k, v in ACCESS.items() if k != KEYCLOAK_ORGANIZATION_CLAIM}

        assert keycloak_organization_alias(claims) is None


# --- GET /api/identity --------------------------------------------------------


class _Services:
    def __init__(self, provisioned, linked) -> None:
        self.resolver = AsyncMock()
        self.resolver.resolve.side_effect = [Error(NotFound("no identity")), Ok(CANONICAL)]
        self.provisioner = AsyncMock()
        self.provisioner.provision.return_value = provisioned
        self.provisioner.organization_tenant_id.return_value = linked


def _get(
    op: LocalProvider, token: str, *, provisioned=Ok("c0de"), linked=Ok(ORGANIZATION_TENANT), jit=("ory", "keycloak")
):
    limiter.reset()
    services = _Services(provisioned, linked)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.include_router(shipped_app.router)
    app.dependency_overrides[get_identity_resolution_service] = lambda: services.resolver
    app.dependency_overrides[get_identity_provisioning_service] = lambda: services.provisioner
    app.add_middleware(TokenValidationMiddleware, providers=[descope_config("P-first-login"), _keycloak(op)])
    original = protected.JIT_ENABLED_PROVIDERS
    protected.JIT_ENABLED_PROVIDERS = frozenset(jit)
    try:
        response = TestClient(app).get(IDENTITY, headers={"Authorization": f"Bearer {token}"})
    finally:
        protected.JIT_ENABLED_PROVIDERS = original
    return response, services


class TestAFirstLogin:
    def test_is_provisioned_into_the_aliases_tenant_with_the_access_tokens_profile(self, op):
        response, services = _get(op, _token(op))

        assert response.status_code == 200, response.text
        assert response.json() == CANONICAL
        services.provisioner.organization_tenant_id.assert_awaited_once_with("keycloak", ALIAS)
        services.provisioner.provision.assert_awaited_once_with(
            provider_name="keycloak",
            sub=SUB,
            email=ACCESS["email"],
            email_verified=True,
            given_name=ACCESS["given_name"],
            family_name=ACCESS["family_name"],
            assign_default_role=True,
            organization_id=ALIAS,
        )

    def test_without_the_organization_claim_is_refused(self, op):
        response, services = _get(op, _token(op, organization=None))

        assert response.status_code == 403
        assert response.json() == {"detail": protected.ORGANIZATION_NOT_LINKED}
        services.provisioner.organization_tenant_id.assert_not_awaited()
        services.provisioner.provision.assert_not_awaited()

    def test_with_two_aliases_is_refused(self, op):
        response, services = _get(op, _token(op, organization=["globex", ALIAS]))

        assert response.status_code == 403
        assert response.json() == {"detail": protected.ORGANIZATION_NOT_LINKED}
        services.provisioner.organization_tenant_id.assert_not_awaited()
        services.provisioner.provision.assert_not_awaited()

    def test_an_alias_no_tenant_names_is_refused(self, op):
        response, services = _get(op, _token(op), linked=Error(Forbidden("not linked")))

        assert response.status_code == 403
        assert response.json() == {"detail": protected.ORGANIZATION_NOT_LINKED}
        services.provisioner.provision.assert_not_awaited()

    @pytest.mark.parametrize("verified", [False, "true", None])
    def test_an_unverified_email_reaches_provisioning_as_unverified_and_is_refused(self, op, verified):
        response, services = _get(op, _token(op, email_verified=verified), provisioned=Error(ValidationError("no")))

        assert response.status_code == 400
        assert services.provisioner.provision.await_args.kwargs["email_verified"] is False

    def test_a_missing_email_reaches_provisioning_as_empty_and_is_refused(self, op):
        response, services = _get(op, _token(op, email=None), provisioned=Error(ValidationError("no email")))

        assert response.status_code == 400
        assert services.provisioner.provision.await_args.kwargs["email"] == ""

    def test_without_the_provider_row_is_500(self, op):
        response, services = _get(op, _token(op), linked=Error(NotFound("no provider row")))

        assert response.status_code == 500
        assert response.json() == {"detail": protected.PROVIDER_NOT_CONFIGURED}
        services.provisioner.provision.assert_not_awaited()

    def test_an_email_another_canonical_user_holds_is_refused_without_saying_so(self, op):
        response, _ = _get(op, _token(op), provisioned=Error(EmailLinkRefused("held")))

        assert response.status_code == 409
        assert response.json() == {"detail": protected.PROVISIONING_FAILED}

    def test_without_jit_nothing_is_provisioned(self, op):
        response, services = _get(op, _token(op), jit=("ory",))

        assert response.status_code == 404
        services.provisioner.organization_tenant_id.assert_not_awaited()
        services.provisioner.provision.assert_not_awaited()

    def test_a_known_user_is_not_provisioned_again(self, op):
        limiter.reset()
        services = _Services(Ok("c0de"), Ok(ORGANIZATION_TENANT))
        services.resolver.resolve.side_effect = [Ok(CANONICAL)]
        app = FastAPI()
        app.state.limiter = limiter
        app.include_router(shipped_app.router)
        app.dependency_overrides[get_identity_resolution_service] = lambda: services.resolver
        app.dependency_overrides[get_identity_provisioning_service] = lambda: services.provisioner
        app.add_middleware(TokenValidationMiddleware, providers=[_keycloak(op)])

        response = TestClient(app).get(IDENTITY, headers={"Authorization": f"Bearer {_token(op)}"})

        assert response.status_code == 200
        assert response.json() == CANONICAL
        services.provisioner.provision.assert_not_awaited()


class TestWithoutTheRecordedConfig:
    def test_a_keycloak_first_login_is_refused(self):
        """Its organization rule is in the config: a principal with none recorded is not provisioned."""
        with pytest.raises(protected.HTTPException) as refused:
            protected._recorded_provider(None, "keycloak")

        assert refused.value.status_code == 500
