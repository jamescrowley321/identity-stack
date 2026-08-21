"""Unit tests for the provider-agnostic token-validation config (app.middleware.providers)."""

import base64
import json

import pytest

from app.middleware.providers import (
    ProviderTokenConfig,
    audience_ok,
    audience_rejected,
    build_provider_configs,
    descope_config,
    infer_single_tenant_dct,
    order_candidates,
    ory_config,
    select_by_issuer,
    unverified_issuer,
)

ORY_ISSUER = "https://inspiring-nash-yli2uiwmcw.projects.oryapis.com"


def _token_with(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


class TestDescopeConfig:
    def test_with_project_id_has_both_issuer_formats(self):
        cfg = descope_config("P123")
        assert cfg.name == "Descope"
        assert cfg.accepted_issuers == frozenset(
            {"https://api.descope.com/P123", "https://api.descope.com/v1/apps/P123"}
        )
        assert cfg.audience == "P123"
        assert cfg.infer_single_tenant_dct is True
        assert cfg.disco_address == "https://api.descope.com/P123/.well-known/openid-configuration"

    def test_without_project_id_skips_issuer_validation(self):
        cfg = descope_config("")
        assert cfg.accepted_issuers == frozenset()
        assert cfg.audience is None


class TestOryConfig:
    def test_config_fields(self):
        cfg = ory_config(ORY_ISSUER, audience="identity-stack-api")
        assert cfg == ProviderTokenConfig(
            name="Ory",
            accepted_issuers=frozenset({ORY_ISSUER}),
            disco_address=f"{ORY_ISSUER}/.well-known/openid-configuration",
            audience="identity-stack-api",
            infer_single_tenant_dct=False,
            require_audience=True,
        )

    def test_trailing_slash_stripped(self):
        cfg = ory_config(ORY_ISSUER + "/", audience="identity-stack")
        assert cfg.accepted_issuers == frozenset({ORY_ISSUER})
        assert cfg.disco_address == f"{ORY_ISSUER}/.well-known/openid-configuration"
        assert cfg.audience == "identity-stack"


class TestBuildProviderConfigs:
    def test_descope_only(self):
        providers = build_provider_configs(descope_project_id="P123")
        assert [p.name for p in providers] == ["Descope"]

    def test_descope_and_ory(self):
        providers = build_provider_configs(
            descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience="identity-stack-api"
        )
        assert [p.name for p in providers] == ["Descope", "Ory"]
        assert providers[1].accepted_issuers == frozenset({ORY_ISSUER})

    def test_descope_always_present_even_without_project_id(self):
        providers = build_provider_configs(ory_issuer_url=ORY_ISSUER, ory_audience="identity-stack-api")
        assert [p.name for p in providers] == ["Descope", "Ory"]
        assert providers[0].accepted_issuers == frozenset()


class TestUnverifiedIssuer:
    def test_reads_issuer(self):
        assert unverified_issuer(_token_with({"iss": ORY_ISSUER, "sub": "u"})) == ORY_ISSUER

    def test_missing_issuer_returns_none(self):
        assert unverified_issuer(_token_with({"sub": "u"})) is None

    def test_non_string_issuer_returns_none(self):
        assert unverified_issuer(_token_with({"iss": 42})) is None

    def test_opaque_token_returns_none(self):
        assert unverified_issuer("valid.mock.token") is None

    def test_non_jwt_returns_none(self):
        assert unverified_issuer("not-a-jwt") is None


class TestOrderCandidates:
    def _providers(self):
        return build_provider_configs(
            descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience="identity-stack-api"
        )

    def test_hint_matches_returns_only_that_provider(self):
        providers = self._providers()
        ordered = order_candidates(providers, ORY_ISSUER)
        assert [p.name for p in ordered] == ["Ory"]

    def test_hint_matches_descope(self):
        providers = self._providers()
        ordered = order_candidates(providers, "https://api.descope.com/v1/apps/P123")
        assert [p.name for p in ordered] == ["Descope"]

    def test_unknown_hint_with_config_returns_empty(self):
        providers = self._providers()
        assert order_candidates(providers, "https://evil.example.com") == []

    def test_unknown_hint_without_config_returns_all(self):
        providers = build_provider_configs(descope_project_id="")  # no allow-lists
        ordered = order_candidates(providers, "https://anything")
        assert [p.name for p in ordered] == ["Descope"]

    def test_no_hint_orders_configured_first(self):
        # Descope-with-no-pid (empty allow-list) must come AFTER a configured Ory
        # so an opaque/mock token is attributed by its returned issuer.
        providers = build_provider_configs(
            descope_project_id="", ory_issuer_url=ORY_ISSUER, ory_audience="identity-stack-api"
        )
        ordered = order_candidates(providers, None)
        assert [p.name for p in ordered] == ["Ory", "Descope"]


class TestSelectByIssuer:
    def test_no_config_returns_first_provider(self):
        providers = build_provider_configs(descope_project_id="")
        assert select_by_issuer(providers, None) is providers[0]

    def test_matches_ory(self):
        providers = build_provider_configs(
            descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience="identity-stack-api"
        )
        assert select_by_issuer(providers, ORY_ISSUER).name == "Ory"

    def test_matches_descope_session_issuer(self):
        providers = build_provider_configs(
            descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience="identity-stack-api"
        )
        assert select_by_issuer(providers, "https://api.descope.com/v1/apps/P123").name == "Descope"

    def test_unknown_issuer_returns_none(self):
        providers = build_provider_configs(descope_project_id="P123")
        assert select_by_issuer(providers, "https://evil.example.com") is None

    def test_non_string_issuer_returns_none(self):
        providers = build_provider_configs(descope_project_id="P123")
        assert select_by_issuer(providers, 42) is None


class TestAudienceOk:
    def test_string_match(self):
        assert audience_ok("P123", "P123") is True

    def test_string_mismatch(self):
        assert audience_ok("P456", "P123") is False

    def test_list_membership(self):
        assert audience_ok(["P123", "other"], "P123") is True

    def test_list_absent(self):
        assert audience_ok(["other"], "P123") is False


class TestRequireAudience:
    def test_descope_does_not_require_audience(self):
        assert descope_config("P123").require_audience is False

    def test_ory_requires_audience_by_default(self):
        assert ory_config(ORY_ISSUER, audience="a").require_audience is True

    def test_build_ory_without_audience_raises(self):
        with pytest.raises(ValueError, match="ORY_AUDIENCE"):
            build_provider_configs(descope_project_id="P123", ory_issuer_url=ORY_ISSUER)

    def test_build_ory_with_audience_ok(self):
        providers = build_provider_configs(ory_issuer_url=ORY_ISSUER, ory_audience="a")
        assert providers[1].require_audience is True and providers[1].audience == "a"

    def test_build_ory_opt_out_allows_no_audience(self):
        providers = build_provider_configs(ory_issuer_url=ORY_ISSUER, ory_require_audience=False)
        assert providers[1].require_audience is False


class TestAudienceRejected:
    def _ory(self):
        return ory_config(ORY_ISSUER, audience="api")

    def test_require_audience_rejects_missing_aud(self):
        assert audience_rejected({"sub": "u"}, self._ory()) is True

    def test_require_audience_rejects_wrong_aud(self):
        assert audience_rejected({"sub": "u", "aud": "other"}, self._ory()) is True

    def test_require_audience_accepts_matching_aud(self):
        assert audience_rejected({"sub": "u", "aud": "api"}, self._ory()) is False
        assert audience_rejected({"sub": "u", "aud": ["api", "x"]}, self._ory()) is False

    def test_non_require_descope_skips_absent_aud(self):
        assert audience_rejected({"sub": "u"}, descope_config("P123")) is False

    def test_non_require_descope_rejects_present_mismatch(self):
        assert audience_rejected({"sub": "u", "aud": "P456"}, descope_config("P123")) is True


class TestInferSingleTenantDct:
    def test_descope_single_tenant_inferred(self):
        claims = {"sub": "u", "tenants": {"t-only": {"roles": ["viewer"]}}}
        infer_single_tenant_dct(claims, descope_config("P123"))
        assert claims["dct"] == "t-only"

    def test_descope_multi_tenant_not_inferred(self):
        claims = {"sub": "u", "tenants": {"t1": {}, "t2": {}}}
        infer_single_tenant_dct(claims, descope_config("P123"))
        assert "dct" not in claims

    def test_existing_dct_untouched(self):
        claims = {"sub": "u", "dct": "explicit", "tenants": {"t1": {}}}
        infer_single_tenant_dct(claims, descope_config("P123"))
        assert claims["dct"] == "explicit"

    def test_ory_provider_is_noop(self):
        claims = {"sub": "u", "tenants": {"t-only": {}}}
        infer_single_tenant_dct(claims, ory_config(ORY_ISSUER))
        assert "dct" not in claims
