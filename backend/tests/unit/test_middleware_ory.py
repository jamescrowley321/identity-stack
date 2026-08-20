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

    app.add_middleware(
        TokenValidationMiddleware,
        excluded_paths={"/api/health"},
        **mw_kwargs,
    )
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
        """An Ory-issued token authenticates and is attributed to the Ory provider."""
        mock_validate.return_value = {"sub": "ory-user", "email": "u@example.com", "iss": ORY_ISSUER}
        app = _build_app(ory_issuer_url=ORY_ISSUER)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "ory-user"}))
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_type"] == "Ory"
        assert data["is_authenticated"] is True
        assert data["sub"] == "ory-user"

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_token_has_no_tenant_without_dct(self, mock_validate):
        """Ory tokens carry no dct/tenants; tenant_id stays None (resolved canonically)."""
        mock_validate.return_value = {"sub": "ory-user", "iss": ORY_ISSUER}
        app = _build_app(ory_issuer_url=ORY_ISSUER)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "ory-user"}))
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] is None

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_does_not_infer_dct_from_tenants(self, mock_validate):
        """Even if a tenants claim is present, the Ory provider does not infer dct."""
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER, "tenants": {"t-only": {}}}
        app = _build_app(ory_issuer_url=ORY_ISSUER)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] is None

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_audience_mismatch_rejected(self, mock_validate):
        """When an Ory audience is configured, a mismatching aud is rejected."""
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER, "aud": "someone-else"}
        app = _build_app(ory_issuer_url=ORY_ISSUER, ory_audience="identity-stack")
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 401

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_audience_match_accepted(self, mock_validate):
        """A matching Ory audience is accepted."""
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER, "aud": ["identity-stack"]}
        app = _build_app(ory_issuer_url=ORY_ISSUER, ory_audience="identity-stack")
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 200
        assert resp.json()["auth_type"] == "Ory"


class TestMultiProviderRouting:
    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_ory_token_routes_to_ory_when_both_configured(self, mock_validate):
        mock_validate.return_value = {"sub": "u", "iss": ORY_ISSUER}
        app = _build_app(descope_project_id="P123", ory_issuer_url=ORY_ISSUER)
        resp = await _get(app, _token({"iss": ORY_ISSUER, "sub": "u"}))
        assert resp.status_code == 200
        assert resp.json()["auth_type"] == "Ory"

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_descope_token_routes_to_descope_when_both_configured(self, mock_validate):
        mock_validate.return_value = {"sub": "u", "iss": DESCOPE_ISSUER, "dct": "t1", "tenants": {"t1": {}}}
        app = _build_app(descope_project_id="P123", ory_issuer_url=ORY_ISSUER)
        resp = await _get(app, _token({"iss": DESCOPE_ISSUER, "sub": "u"}))
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_type"] == "Descope"
        assert data["tenant_id"] == "t1"

    @pytest.mark.anyio
    @patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
    async def test_unknown_issuer_rejected_without_validation(self, mock_validate):
        """A token whose issuer matches no configured provider is rejected up front."""
        app = _build_app(descope_project_id="P123", ory_issuer_url=ORY_ISSUER)
        resp = await _get(app, _token({"iss": "https://evil.example.com", "sub": "u"}))
        assert resp.status_code == 401
        mock_validate.assert_not_awaited()
