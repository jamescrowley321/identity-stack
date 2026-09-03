"""Unit tests for the auth router (logout endpoint)."""

import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi.errors import RateLimitExceeded

from app.main import app
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler
from app.routers import auth as auth_router

_mock_project_id = os.getenv("DESCOPE_PROJECT_ID", "test-project-id")
_mock_issuer = f"https://api.descope.com/{_mock_project_id}"

_ORY_ISSUER = "https://inspiring-nash-yli2uiwmcw.projects.oryapis.com"
_ORY_AUD = "https://identity-stack-api"
_ORY_END_SESSION = f"{_ORY_ISSUER}/oauth2/sessions/logout"


def _jwt(payload: dict) -> str:
    """Build an unsigned JWT whose payload is base64-decodable (for the iss hint)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _ory_app() -> FastAPI:
    """Minimal app that mounts the auth router with Ory configured as a provider.

    ``app.main`` builds its middleware provider list at import time (before the
    test sets ORY_ISSUER_URL), so a purpose-built app is used to exercise the Ory
    principal path — mirroring tests/unit/test_middleware_ory.py.
    """
    ory_app = FastAPI()
    ory_app.state.limiter = limiter
    ory_app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    ory_app.include_router(auth_router.router, prefix="/api")
    ory_app.add_middleware(
        TokenValidationMiddleware,
        descope_project_id="test-project-id",
        ory_issuer_url=_ORY_ISSUER,
        ory_audience=_ORY_AUD,
    )
    return ory_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_logout_rejects_missing_auth(client):
    """Logout should return 401 without Authorization header."""
    response = await client.post("/api/auth/logout")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_logout_rejects_invalid_token(client):
    """Logout should return 401 for an invalid JWT."""
    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.services.logout.httpx.AsyncClient")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_succeeds_with_valid_token(mock_validate, mock_httpx_cls, client):
    """Logout should return 200, call Descope logout API, and return the user's sub."""
    mock_claims = {
        "sub": "user123",
        "email": "test@example.com",
        "name": "Test User",
        "iss": _mock_issuer,
    }
    mock_validate.return_value = mock_claims

    mock_client = AsyncMock()
    mock_httpx_cls.return_value.__aenter__.return_value = mock_client

    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer valid.mock.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "logged_out"
    assert data["sub"] == "user123"

    mock_client.post.assert_called_once_with(
        "https://api.descope.com/v1/mgmt/user/logout",
        headers={"Authorization": "Bearer test-project-id:test-management-key"},
        json={"userId": "user123"},
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_returns_null_sub_when_missing(mock_validate, client):
    """Logout should handle tokens without a sub claim."""
    mock_claims = {
        "email": "test@example.com",
        "iss": _mock_issuer,
    }
    mock_validate.return_value = mock_claims

    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer valid.mock.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "logged_out"
    assert data["sub"] is None


@pytest.mark.anyio
@patch("app.services.logout.httpx.AsyncClient")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_skips_api_call_without_management_key(mock_validate, mock_httpx_cls, client, monkeypatch):
    """Logout should skip the Descope API call when management key is not set."""
    monkeypatch.delenv("DESCOPE_MANAGEMENT_KEY")
    mock_claims = {"sub": "user123"}
    mock_validate.return_value = mock_claims

    response = await client.post(
        "/api/auth/logout",
        headers={"Authorization": "Bearer valid.mock.token"},
    )
    assert response.status_code == 200
    mock_httpx_cls.return_value.__aenter__.return_value.post.assert_not_called()


@pytest.mark.anyio
@patch("app.services.logout.get_discovery_document", new_callable=AsyncMock)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_ory_returns_rp_initiated_url(mock_validate, mock_disco, monkeypatch):
    """An Ory principal → RP-initiated logout URL with id_token_hint + redirect (AC-1/AC-2)."""
    monkeypatch.setenv("ORY_ISSUER_URL", _ORY_ISSUER)
    monkeypatch.setenv("ORY_AUDIENCE", _ORY_AUD)
    monkeypatch.setenv("ORY_CLIENT_ID", "spa-client-id")
    monkeypatch.setenv("ORY_POST_LOGOUT_REDIRECT_URI", "http://localhost:3000/login")

    mock_validate.return_value = {"sub": "ory-user", "iss": _ORY_ISSUER, "aud": _ORY_AUD}
    mock_disco.return_value = SimpleNamespace(is_successful=True, end_session_endpoint=_ORY_END_SESSION)

    transport = ASGITransport(app=_ory_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {_jwt({'iss': _ORY_ISSUER, 'sub': 'ory-user'})}"},
            json={"id_token": "the-id-token"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "logout_redirect"
    assert data["sub"] == "ory-user"
    assert data["logout_url"].startswith(_ORY_END_SESSION)
    assert "id_token_hint=the-id-token" in data["logout_url"]
    assert "post_logout_redirect_uri=" in data["logout_url"]
    assert "client_id=spa-client-id" in data["logout_url"]
    # Discovery was consulted against Ory's issuer, not Descope's.
    assert mock_disco.await_args.args[0].address.startswith(_ORY_ISSUER)


@pytest.mark.anyio
@patch("app.services.logout.get_discovery_document", new_callable=AsyncMock)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_ory_without_id_token_omits_hint(mock_validate, mock_disco, monkeypatch):
    """Ory logout still succeeds when the SPA sends no id_token (hint omitted, no crash)."""
    monkeypatch.setenv("ORY_ISSUER_URL", _ORY_ISSUER)
    monkeypatch.setenv("ORY_AUDIENCE", _ORY_AUD)

    mock_validate.return_value = {"sub": "ory-user", "iss": _ORY_ISSUER, "aud": _ORY_AUD}
    mock_disco.return_value = SimpleNamespace(is_successful=True, end_session_endpoint=_ORY_END_SESSION)

    transport = ASGITransport(app=_ory_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {_jwt({'iss': _ORY_ISSUER, 'sub': 'ory-user'})}"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "logout_redirect"
    assert "id_token_hint=" not in data["logout_url"]


@pytest.mark.anyio
@patch("app.services.logout.get_discovery_document", new_callable=AsyncMock)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_ory_no_end_session_endpoint_fails_closed(mock_validate, mock_disco, monkeypatch):
    """Fail closed: no end_session_endpoint → 500, never a 200 with no URL (AC-2/E3)."""
    monkeypatch.setenv("ORY_ISSUER_URL", _ORY_ISSUER)
    monkeypatch.setenv("ORY_AUDIENCE", _ORY_AUD)

    mock_validate.return_value = {"sub": "ory-user", "iss": _ORY_ISSUER, "aud": _ORY_AUD}
    mock_disco.return_value = SimpleNamespace(is_successful=False, end_session_endpoint=None)

    transport = ASGITransport(app=_ory_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {_jwt({'iss': _ORY_ISSUER, 'sub': 'ory-user'})}"},
            json={"id_token": "the-id-token"},
        )

    assert response.status_code == 500
