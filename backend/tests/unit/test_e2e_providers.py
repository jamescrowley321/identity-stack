"""The shared e2e suite's providers fail a run that cannot start, never skip it (``tests/e2e/helpers/providers.py``)."""

import base64
import json
import re
from pathlib import Path

import pytest

from tests.e2e import not_applicable as na
from tests.e2e.helpers import providers
from tests.helpers import keycloak_model

UNREACHABLE = "http://127.0.0.1:9"


class TestDescopeCredentialsInCI:
    """The Descope run's CI check, unchanged by the move into the provider."""

    @pytest.fixture
    def descope(self):
        return providers.DescopeProvider(UNREACHABLE, UNREACHABLE)

    def test_a_missing_credential_fails_a_ci_run_naming_it(self, descope, monkeypatch):
        monkeypatch.setenv("CI", "true")
        for name in providers.DESCOPE_CI_REQUIRED_ENV:
            monkeypatch.setenv(name, "set")
        monkeypatch.delenv("DESCOPE_CLIENT_SECRET")
        monkeypatch.setenv("E2E_TEST_EMAIL", "")

        with pytest.raises(
            pytest.fail.Exception, match=r"E2E credentials missing in CI: DESCOPE_CLIENT_SECRET, E2E_TEST_EMAIL\."
        ):
            descope.check_ready(None)

    def test_every_credential_present_passes(self, descope, monkeypatch):
        monkeypatch.setenv("CI", "true")
        for name in providers.DESCOPE_CI_REQUIRED_ENV:
            monkeypatch.setenv(name, "set")

        assert descope.check_ready(None) is None

    def test_outside_ci_nothing_is_required(self, descope, monkeypatch):
        monkeypatch.delenv("CI", raising=False)
        for name in providers.DESCOPE_CI_REQUIRED_ENV:
            monkeypatch.delenv(name, raising=False)

        assert descope.check_ready(None) is None


class TestDescopeCredentialsMissing:
    @pytest.mark.parametrize(
        ("environment", "missing"),
        [
            ({}, True),
            ({"DESCOPE_MANAGEMENT_KEY": "k"}, True),
            ({"DESCOPE_MANAGEMENT_KEY": "k", "DESCOPE_CLIENT_ID": ""}, True),
            ({"DESCOPE_MANAGEMENT_KEY": "k", "DESCOPE_CLIENT_ID": "c"}, False),
            ({"E2E_PROVIDER": "keycloak"}, False),
            ({"E2E_PROVIDER": "zitadel"}, False),
        ],
    )
    def test_only_a_descope_run_skips_on_missing_descope_credentials(self, environment, missing):
        assert (
            na.descope_credentials_missing("DESCOPE_MANAGEMENT_KEY", "DESCOPE_CLIENT_ID", environment=environment)
            is missing
        )


@pytest.fixture
def internal_endpoint_secrets(monkeypatch):
    for name in na.INTERNAL_ENDPOINT_SECRETS:
        monkeypatch.setenv(name, "set")


@pytest.mark.usefixtures("internal_endpoint_secrets")
class TestNoInstanceFails:
    def test_zitadel_without_outputs_fails_the_run(self, monkeypatch):
        monkeypatch.delenv("ZITADEL_TERRAFORM_OUTPUTS", raising=False)

        with pytest.raises(pytest.fail.Exception, match="ZITADEL_TERRAFORM_OUTPUTS is not set"):
            providers.ZitadelProvider(UNREACHABLE, UNREACHABLE).check_ready(None)

    def test_keycloak_unreachable_fails_the_run(self, monkeypatch):
        provider = providers.KeycloakProvider(UNREACHABLE, UNREACHABLE)
        monkeypatch.setattr(provider._keycloak, "KEYCLOAK_BASE_URL", UNREACHABLE)
        source = provider._keycloak.KeycloakTokenSource
        monkeypatch.setattr(provider._keycloak, "KeycloakTokenSource", lambda: source(base_url=UNREACHABLE))

        with pytest.raises(pytest.fail.Exception, match="keycloak-test is not reachable"):
            provider.check_ready(None)


def test_every_provider_has_an_implementation():
    assert set(providers.PROVIDER_CLASSES) == set(na.PROVIDERS)
    assert {name: cls.name for name, cls in providers.PROVIDER_CLASSES.items()} == {name: name for name in na.PROVIDERS}


class TestInternalEndpointSecretsFailTheRun:
    """The internal-endpoint tests skip without their secrets, so a Keycloak or Zitadel run fails first."""

    @pytest.mark.parametrize("cls", [providers.KeycloakProvider, providers.ZitadelProvider])
    def test_a_missing_secret_fails_the_run_naming_it(self, cls, monkeypatch):
        for name in na.INTERNAL_ENDPOINT_SECRETS:
            monkeypatch.setenv(name, "set")
        monkeypatch.delenv("INTERNAL_IDENTITY_KEY")

        with pytest.raises(pytest.fail.Exception, match=r"unset: INTERNAL_IDENTITY_KEY\b"):
            cls(UNREACHABLE, UNREACHABLE).check_ready(None)


def _jwt(claims: dict) -> str:
    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{segment({'alg': 'none'})}.{segment(claims)}.signature"


class TestSignedInTokens:
    """A principal's token is reused until it is about to expire, then the principal signs in again."""

    @pytest.fixture
    def clock(self):
        return {"now": 1_000.0}

    @pytest.fixture
    def tokens(self, clock):
        return providers.SignedInTokens(clock=lambda: clock["now"])

    @pytest.fixture
    def sign_ins(self, clock):
        issued: list[str] = []

        def sign_in() -> str:
            issued.append(_jwt({"exp": clock["now"] + 300}))
            return issued[-1]

        return issued, sign_in

    def test_a_fresh_token_is_reused(self, tokens, sign_ins, clock):
        issued, sign_in = sign_ins
        first = tokens.get(providers.MEMBER, sign_in)
        clock["now"] += 300 - providers.SignedInTokens.MARGIN_SECONDS - 1

        assert tokens.get(providers.MEMBER, sign_in) == first
        assert issued == [first]

    def test_a_token_about_to_expire_is_replaced(self, tokens, sign_ins, clock):
        issued, sign_in = sign_ins
        first = tokens.get(providers.MEMBER, sign_in)
        clock["now"] += 300 - providers.SignedInTokens.MARGIN_SECONDS

        second = tokens.get(providers.MEMBER, sign_in)

        assert issued == [first, second]
        assert second != first

    def test_principals_have_their_own_tokens(self, tokens, sign_ins):
        issued, sign_in = sign_ins
        member = tokens.get(providers.MEMBER, sign_in)
        admin = tokens.get(providers.ADMIN, sign_in)

        assert {providers.MEMBER: member, providers.ADMIN: admin} == {
            providers.MEMBER: issued[0],
            providers.ADMIN: issued[1],
        }

    def test_a_token_without_exp_fails(self, tokens):
        def sign_in() -> str:
            return _jwt({"sub": "someone"})

        with pytest.raises(providers.UnreadableAccessToken, match="no exp claim"):
            tokens.get(providers.MEMBER, sign_in)

    @pytest.mark.parametrize(
        ("token", "cause"),
        [
            ("opaque-access-token", "not a JWT"),
            ("header.!!!.signature", "not a JWT"),
            (f"header.{base64.urlsafe_b64encode(b'[1, 2]').decode()}.signature", "not a JWT"),
            (_jwt({"exp": "soon"}), "no exp claim"),
        ],
        ids=["opaque", "undecodable-payload", "payload-not-an-object", "exp-not-a-number"],
    )
    def test_an_unreadable_token_signs_in_once_across_two_calls(self, tokens, token, cause):
        calls: list[str] = []

        def sign_in() -> str:
            calls.append("sign_in")
            return token

        with pytest.raises(providers.UnreadableAccessToken, match=cause) as first:
            tokens.get(providers.MEMBER, sign_in)
        with pytest.raises(providers.EarlierSignInFailed, match="member principal failed earlier") as second:
            tokens.get(providers.MEMBER, sign_in)

        assert calls == ["sign_in"]
        assert second.value.__cause__ is first.value


class TestAFailedSignInIsNotRetried:
    """A principal whose sign-in failed fails every later test at once, as the session fixtures did."""

    @staticmethod
    def _failing(calls: list[str]):
        def sign_in() -> str:
            calls.append("sign_in")
            raise RuntimeError("Token request failed: invalid_client")

        return sign_in

    def test_descope_signs_in_once_across_two_calls(self, monkeypatch):
        descope = providers.DescopeProvider(UNREACHABLE, UNREACHABLE)
        calls: list[str] = []
        monkeypatch.setattr(descope, "_new_token", lambda principal: self._failing(calls)())

        with pytest.raises(RuntimeError, match="invalid_client") as first:
            descope.access_token(providers.ADMIN, None)
        with pytest.raises(providers.EarlierSignInFailed, match="admin principal failed earlier") as second:
            descope.access_token(providers.ADMIN, None)

        assert calls == ["sign_in"]
        assert second.value.__cause__ is first.value

    def test_descope_principals_fail_apart(self, monkeypatch):
        descope = providers.DescopeProvider(UNREACHABLE, UNREACHABLE)
        token = _jwt({"exp": 2_000_000_000})

        def new_token(principal: str) -> str:
            if principal == providers.ADMIN:
                raise RuntimeError("Access key creation returned no cleartext")
            return token

        monkeypatch.setattr(descope, "_new_token", new_token)

        with pytest.raises(RuntimeError, match="no cleartext"):
            descope.access_token(providers.ADMIN, None)

        assert descope.access_token(providers.MEMBER, None) == token

    def test_a_descope_skip_is_not_recorded(self, monkeypatch):
        descope = providers.DescopeProvider(UNREACHABLE, UNREACHABLE)
        monkeypatch.delenv("DESCOPE_MANAGEMENT_KEY", raising=False)

        for _ in range(2):
            with pytest.raises(pytest.skip.Exception, match="DESCOPE_MANAGEMENT_KEY not set"):
                descope.access_token(providers.ADMIN, None)

    def test_signed_in_tokens_sign_in_once_across_two_calls(self):
        tokens = providers.SignedInTokens(clock=lambda: 1_000.0)
        calls: list[str] = []

        with pytest.raises(RuntimeError, match="invalid_client"):
            tokens.get(providers.MEMBER, self._failing(calls))
        with pytest.raises(providers.EarlierSignInFailed, match="member principal failed earlier"):
            tokens.get(providers.MEMBER, self._failing(calls))

        assert calls == ["sign_in"]


@pytest.fixture(params=[providers.KeycloakProvider, providers.ZitadelProvider], ids=["keycloak", "zitadel"])
def two_tenant(request, monkeypatch):
    provider = request.param(UNREACHABLE, UNREACHABLE)
    monkeypatch.setattr(provider, "_signed_in_token", lambda: "token")
    monkeypatch.setattr(type(provider), "_scope", property(lambda self: "openid organization"))
    return provider


class TestSeedMissing:
    """A run whose linked user is not in both tenants fails naming the user, its scope, the seed and the answer."""

    def _memberships(self, provider, monkeypatch, names):
        answer = {
            "tenant_memberships": [{"tenant_name": name, "tenant_id": f"id-{name}"} for name in names],
        }
        monkeypatch.setattr(provider, "_identity", lambda token, headers=None: answer)
        return answer

    def _fixture_tenants(self, provider):
        return sorted({provider._tenant_key(providers.ADMIN), provider._tenant_key(providers.MEMBER)})

    def test_both_tenants_present_give_their_ids(self, two_tenant, monkeypatch):
        self._memberships(two_tenant, monkeypatch, self._fixture_tenants(two_tenant))

        assert {principal: two_tenant.headers(principal) for principal in providers.PRINCIPALS} == {
            principal: {"X-Tenant-ID": f"id-{two_tenant._tenant_key(principal)}"} for principal in providers.PRINCIPALS
        }

    @pytest.mark.parametrize("principal", [providers.ADMIN, providers.MEMBER])
    def test_an_unseeded_provider_fails_naming_the_seed(self, two_tenant, monkeypatch, principal):
        answer = self._memberships(two_tenant, monkeypatch, [])
        missing = ", ".join(self._fixture_tenants(two_tenant))

        with pytest.raises(pytest.fail.Exception) as failed:
            two_tenant.headers(principal)

        assert str(failed.value) == (
            f"the linked user {two_tenant._linked.key}, both principals (signed in with scope 'openid organization'), "
            f"is not a member of {missing}: run `{two_tenant.seed_step}` first. GET /api/identity answered {answer}"
        )

    def test_the_lookup_fails_once_and_every_later_call_with_the_same_message(self, two_tenant, monkeypatch):
        calls: list[str] = []
        answer = {"tenant_memberships": []}

        def identity(token, headers=None):
            calls.append(token)
            return answer

        monkeypatch.setattr(two_tenant, "_identity", identity)
        with pytest.raises(pytest.fail.Exception) as first:
            two_tenant.headers(providers.ADMIN)
        with pytest.raises(pytest.fail.Exception) as second:
            two_tenant.tenant_id(None)

        assert (calls, str(second.value)) == (["token"], f"{first.value} (found earlier in this run)")

    def test_one_missing_tenant_is_named(self, two_tenant, monkeypatch):
        admin_tenant = two_tenant._tenant_key(providers.ADMIN)
        member_tenant = two_tenant._tenant_key(providers.MEMBER)
        self._memberships(two_tenant, monkeypatch, [admin_tenant])

        with pytest.raises(pytest.fail.Exception, match=rf"not a member of {member_tenant}: run `"):
            two_tenant.tenant_id(None)


def test_each_provider_names_its_own_seed():
    assert {cls.name: cls.seed_step for cls in (providers.KeycloakProvider, providers.ZitadelProvider)} == {
        na.KEYCLOAK: "make keycloak-seed",
        na.ZITADEL: "make zitadel-seed",
    }


def test_the_keycloak_principals_sign_in_with_the_scope_the_spa_is_built_with():
    outputs = (Path(__file__).resolve().parents[3] / "infra" / "keycloak" / "outputs.tf").read_text()
    (scope,) = re.findall(r'VITE_OIDC_SCOPE\s*=\s*"([^"]*)"', outputs)

    assert keycloak_model.SPA_SCOPE == scope
