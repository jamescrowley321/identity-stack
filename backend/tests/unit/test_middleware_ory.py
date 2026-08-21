"""Standalone TokenValidationMiddleware: Ory provider + multi-provider routing.

Builds a minimal app with the middleware directly (rather than app.main) so Ory
and Descope+Ory configurations can be exercised. ``validate_token`` is patched —
it stands in for py-identity-model's signature verification — so these tests
cover provider selection, issuer attribution, audience, and dct handling, not
the crypto itself.
"""

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from app.middleware.auth import TokenValidationMiddleware

ORY_ISSUER = "https://inspiring-nash-yli2uiwmcw.projects.oryapis.com"
DESCOPE_ISSUER = "https://api.descope.com/P123"
ORY_AUD = "identity-stack-api"


def _token(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _build_app(**mw_kwargs) -> FastAPI:
    app = FastAPI()

    @app.get("/api/protected")
    async def protected(request: Request):
        identity = request.state.principal.identity
        return JSONResponse(
            {
                "auth_type": identity.authentication_type,
                "is_authenticated": identity.is_authenticated(),
                "tenant_id": request.state.tenant_id,
                "sub": request.state.claims.get("sub"),
            }
        )

    app.add_middleware(TokenValidationMiddleware, excluded_paths={"/api/health"}, **mw_kwargs)
    return app


async def _get(app: FastAPI, token: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TestOryProvider:
    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_valid_ory_token_attributed_to_ory(self, mock_validate):
        """An Ory-issued token (with a matching aud) authenticates and is attributed to Ory."""
        mock_validate.return_value = {"sub": "ory-user", "email": "u@example.com", "iss": ORY_ISSUER, "aud": ORY_AUD}
        app = _build_app(ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "ory-user"}))
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_type"] == "Ory"
        assert data["is_authenticated"] is True
        assert data["sub"] == "ory-user"

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_token_has_no_tenant_without_dct(self, mock_validate):
        mock_validate.return_value = {"sub": "ory-user", "iss": ORY_ISSUER, "aud": ORY_AUD}
        app = _build_app(ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "ory-user"}))
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] is None

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_does_not_infer_dct_from_tenants(self, mock_validate):
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER, "aud": ORY_AUD, "tenants": {"t-only": {}}}
        app = _build_app(ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] is None

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_missing_aud_rejected(self, mock_validate):
        """Fail closed: a genuine Ory token that carries no aud is rejected."""
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER}  # no aud
        app = _build_app(ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 401

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_audience_mismatch_rejected(self, mock_validate):
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER, "aud": "someone-else"}
        app = _build_app(ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 401

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_audience_match_accepted(self, mock_validate):
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER, "aud": [ORY_AUD]}
        app = _build_app(ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 200
        assert resp.json()["auth_type"] == "Ory"


class TestMultiProviderRouting:
    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_token_routes_to_ory_when_both_configured(self, mock_validate):
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER, "aud": ORY_AUD}
        app = _build_app(descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 200
        assert resp.json()["auth_type"] == "Ory"

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_descope_token_routes_to_descope_when_both_configured(self, mock_validate):
        mock_validate.return_value = {"sub": "u", "iss": DESCOPE_ISSUER, "dct": "t1", "tenants": {"t1": {}}}
        app = _build_app(descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": DESCOPE_ISSUER, "sub": "u"}))
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_type"] == "Descope"
        assert data["tenant_id"] == "t1"

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_crypto_fallthrough_attributes_to_second_provider(self, mock_validate):
        """An opaque (no-iss-hint) token: the first candidate's JWKS rejects it
        (validate_token raises); the loop falls through to the provider whose keys
        actually verify it, and attribution follows the verified issuer."""
        mock_validate.side_effect = [Exception("wrong JWKS"), {"sub": "u", "iss": ORY_ISSUER, "aud": ORY_AUD}]
        app = _build_app(descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"sub": "u"}))  # no iss → tries providers in order
        assert resp.status_code == 200
        assert resp.json()["auth_type"] == "Ory"
        assert mock_validate.await_count == 2

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_all_candidates_fail_signature_returns_401(self, mock_validate):
        mock_validate.side_effect = Exception("bad signature")
        app = _build_app(descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"sub": "u"}))
        assert resp.status_code == 401

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_unknown_issuer_rejected_without_validation(self, mock_validate):
        app = _build_app(descope_project_id="P123", ory_issuer_url=ORY_ISSUER, ory_audience=ORY_AUD)
        resp = await _get(app, _token({"iss": "https://evil.example.com", "sub": "u"}))
        assert resp.status_code == 401
        mock_validate.assert_not_awaited()
