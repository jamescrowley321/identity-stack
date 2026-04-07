"""Unit tests for Tyk API definition (Story 1.2).

Validates that tyk/apps/saas-backend.json satisfies all acceptance criteria
for backend proxy configuration with Descope JWT validation.
"""

import json
from pathlib import Path

# Repo root is two levels up from tests/unit/
REPO_ROOT = Path(__file__).resolve().parents[2].parent
TYK_APPS_DIR = REPO_ROOT / "tyk" / "apps"
API_DEF_FILE = TYK_APPS_DIR / "saas-backend.json"


def _load_api_def() -> dict:
    """Load and parse saas-backend.json."""
    return json.loads(API_DEF_FILE.read_text())


class TestApiDefinitionJsonValidity:
    """Prerequisite: the file must be valid JSON."""

    def test_file_exists(self):
        assert API_DEF_FILE.exists(), f"{API_DEF_FILE} does not exist"

    def test_valid_json(self):
        api_def = _load_api_def()
        assert isinstance(api_def, dict)


class TestAC1ProxyRouting:
    """AC1: listen_path /api/ -> backend:8000/api/, strip_listen_path false, preserve_host_header true."""

    def test_listen_path(self):
        api_def = _load_api_def()
        assert api_def["listen_path"] == "/api/"

    def test_target_url(self):
        api_def = _load_api_def()
        assert api_def["target_url"] == "http://backend:8000/api/"

    def test_strip_listen_path_false(self):
        api_def = _load_api_def()
        assert api_def["strip_listen_path"] is False

    def test_preserve_host_header(self):
        api_def = _load_api_def()
        assert api_def["proxy"]["preserve_host_header"] is True

    def test_proxy_listen_path_matches_top_level(self):
        api_def = _load_api_def()
        assert api_def["proxy"]["listen_path"] == api_def["listen_path"]

    def test_proxy_target_url_matches_top_level(self):
        api_def = _load_api_def()
        assert api_def["proxy"]["target_url"] == api_def["target_url"]

    def test_proxy_strip_listen_path_matches_top_level(self):
        api_def = _load_api_def()
        assert api_def["proxy"]["strip_listen_path"] == api_def["strip_listen_path"]

    def test_api_is_active(self):
        api_def = _load_api_def()
        assert api_def["active"] is True


class TestAC2DualIssuerOpenID:
    """AC2: use_openid true with dual-issuer Descope providers."""

    def test_use_openid_true(self):
        api_def = _load_api_def()
        assert api_def["use_openid"] is True

    def test_enable_jwt_not_used(self):
        """ADR-GW-1: use_openid, NOT enable_jwt (single source only)."""
        api_def = _load_api_def()
        assert "enable_jwt" not in api_def or api_def.get("enable_jwt") is not True

    def test_two_providers(self):
        api_def = _load_api_def()
        providers = api_def["openid_options"]["providers"]
        assert len(providers) == 2

    def test_oidc_issuer_format(self):
        """First provider: https://api.descope.com/{project_id}."""
        api_def = _load_api_def()
        providers = api_def["openid_options"]["providers"]
        issuer = providers[0]["issuer"]
        assert issuer.startswith("https://api.descope.com/")
        assert "/v1/apps/" not in issuer

    def test_session_token_issuer_format(self):
        """Second provider: https://api.descope.com/v1/apps/{project_id}."""
        api_def = _load_api_def()
        providers = api_def["openid_options"]["providers"]
        issuer = providers[1]["issuer"]
        assert "https://api.descope.com/v1/apps/" in issuer

    def test_providers_have_client_ids(self):
        api_def = _load_api_def()
        providers = api_def["openid_options"]["providers"]
        for provider in providers:
            assert "client_ids" in provider
            assert len(provider["client_ids"]) > 0

    def test_segregate_by_client_false(self):
        api_def = _load_api_def()
        assert api_def["openid_options"]["segregate_by_client"] is False


class TestAC3JwtIdentityBaseField:
    """AC3: jwt_identity_base_field set to 'sub'."""

    def test_jwt_identity_base_field_is_sub(self):
        api_def = _load_api_def()
        assert api_def["jwt_identity_base_field"] == "sub"


class TestAC4AuthorizationHeaderForwarded:
    """AC4: Authorization header NOT stripped — forwarded to backend."""

    def test_strip_auth_data_false(self):
        """strip_auth_data must be explicitly false."""
        api_def = _load_api_def()
        assert "strip_auth_data" in api_def, "strip_auth_data must be explicitly set"
        assert api_def["strip_auth_data"] is False


class TestAC5AuthBoundaryDocumented:
    """AC5: x-description documents the auth/authz boundary."""

    def test_x_description_exists(self):
        api_def = _load_api_def()
        assert "x-description" in api_def

    def test_x_description_mentions_tyk_validation(self):
        api_def = _load_api_def()
        desc = api_def["x-description"].lower()
        assert "tyk" in desc

    def test_x_description_mentions_fastapi_authorization(self):
        api_def = _load_api_def()
        desc = api_def["x-description"].lower()
        assert "fastapi" in desc

    def test_x_description_mentions_authorization_header(self):
        api_def = _load_api_def()
        desc = api_def["x-description"].lower()
        assert "authorization" in desc


class TestAC6DescopeProjectIdParameterized:
    """AC6: DESCOPE_PROJECT_ID is parameterized (placeholder, not hardcoded)."""

    def test_oidc_issuer_uses_placeholder(self):
        api_def = _load_api_def()
        providers = api_def["openid_options"]["providers"]
        issuer = providers[0]["issuer"]
        assert "${DESCOPE_PROJECT_ID}" in issuer

    def test_session_issuer_uses_placeholder(self):
        api_def = _load_api_def()
        providers = api_def["openid_options"]["providers"]
        issuer = providers[1]["issuer"]
        assert "${DESCOPE_PROJECT_ID}" in issuer

    def test_client_ids_use_placeholder(self):
        api_def = _load_api_def()
        providers = api_def["openid_options"]["providers"]
        for provider in providers:
            assert "${DESCOPE_PROJECT_ID}" in str(provider["client_ids"])

    def test_no_hardcoded_project_id(self):
        """Ensure no real Descope project ID is hardcoded (format: P...)."""
        content = API_DEF_FILE.read_text()
        # DESCOPE_PROJECT_ID placeholders are expected; real IDs start with P and are 20+ chars
        import re

        # Remove the placeholder references before checking
        cleaned = content.replace("${DESCOPE_PROJECT_ID}", "")
        # Real Descope project IDs are alphanumeric strings 20+ chars
        matches = re.findall(r"P[a-zA-Z0-9]{19,}", cleaned)
        assert len(matches) == 0, f"Possible hardcoded project ID found: {matches}"
