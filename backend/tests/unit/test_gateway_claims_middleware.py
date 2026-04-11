"""Unit tests for GatewayClaimsMiddleware (gateway-mode JWT claims extraction)."""

import base64
import json
import time

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from app.dependencies.rbac import require_role
from app.middleware.claims import GatewayClaimsMiddleware


def _future_exp(seconds_ahead: int = 3600) -> int:
    return int(time.time()) + seconds_ahead


def _make_token(payload: dict, *, auto_exp: bool = True) -> str:
    """Build an unsigned JWT with the given payload.

    By default a valid future ``exp`` is injected if the caller did not
    supply one — most tests don't care about the exp check and just want
    a payload that flows through the middleware.
    """
    if auto_exp and "exp" not in payload:
        payload = {**payload, "exp": _future_exp()}
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.fakesig"


def _build_app(
    excluded_paths: set[str] | None = None,
    excluded_prefixes: set[str] | None = None,
    descope_project_id: str = "",
) -> FastAPI:
    """Build a minimal FastAPI app with GatewayClaimsMiddleware."""
    app = FastAPI()

    @app.get("/api/protected")
    async def protected(request: Request):
        return JSONResponse(
            {
                "claims": request.state.claims,
                "has_principal": request.state.principal is not None,
                "tenant_id": request.state.tenant_id,
            }
        )

    @app.get("/api/health")
    async def health():
        return JSONResponse({"status": "ok"})

    app.add_middleware(
        GatewayClaimsMiddleware,
        descope_project_id=descope_project_id,
        excluded_paths=excluded_paths or {"/api/health"},
        excluded_prefixes=excluded_prefixes,
    )
    return app


@pytest.fixture
def app():
    return _build_app()


@pytest.fixture
def client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestClaimsExtraction:
    """Test JWT payload decoding and request.state population."""

    @pytest.mark.anyio
    async def test_decodes_valid_jwt_sets_claims(self, client):
        """Valid JWT populates request.state.claims with decoded payload."""
        payload = {"sub": "user123", "dct": "t1", "tenants": {"t1": {"roles": ["admin"]}}}
        token = _make_token(payload)
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["claims"]["sub"] == "user123"
        assert data["claims"]["dct"] == "t1"
        assert data["tenant_id"] == "t1"

    @pytest.mark.anyio
    async def test_sets_principal(self, client):
        """Valid JWT populates request.state.principal via to_principal."""
        payload = {"sub": "user123", "dct": "t1", "tenants": {"t1": {}}}
        token = _make_token(payload)
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_principal"] is True

    @pytest.mark.anyio
    async def test_infers_dct_for_single_tenant_access_key(self, client):
        """When dct is missing but exactly one tenant exists, infer dct."""
        payload = {"sub": "ak123", "tenants": {"tenant-only": {"roles": ["viewer"]}}}
        token = _make_token(payload)
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["claims"]["dct"] == "tenant-only"
        assert data["tenant_id"] == "tenant-only"

    @pytest.mark.anyio
    async def test_no_dct_inference_for_multi_tenant(self, client):
        """When dct is missing and multiple tenants exist, do not infer."""
        payload = {"sub": "ak123", "tenants": {"t1": {}, "t2": {}}}
        token = _make_token(payload)
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert "dct" not in data["claims"]
        assert data["tenant_id"] is None


class TestExcludedPaths:
    """Test that excluded paths bypass claims processing."""

    @pytest.mark.anyio
    async def test_excluded_path_bypasses_middleware(self, client):
        """Requests to excluded paths pass through without auth."""
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    @pytest.mark.anyio
    async def test_excluded_prefix_bypasses_middleware(self):
        """Requests matching excluded prefixes pass through without auth."""
        app = _build_app(excluded_prefixes={"/api/internal/"})

        @app.get("/api/internal/status")
        async def internal_status():
            return JSONResponse({"status": "internal"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/internal/status")
            assert resp.status_code == 200


class TestErrorHandling:
    """Test error responses for missing/malformed authorization."""

    @pytest.mark.anyio
    async def test_missing_authorization_header(self, client):
        """Missing Authorization header returns 401."""
        resp = await client.get("/api/protected")
        assert resp.status_code == 401
        assert "Missing or invalid authorization header" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_empty_authorization_header(self, client):
        """Empty Authorization header returns 401."""
        resp = await client.get("/api/protected", headers={"Authorization": ""})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_non_bearer_scheme(self, client):
        """Non-Bearer auth scheme returns 401."""
        resp = await client.get("/api/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_malformed_jwt_not_three_segments(self, client):
        """Token without 3 dot-separated segments returns 401."""
        resp = await client.get("/api/protected", headers={"Authorization": "Bearer not.a.valid.jwt.token"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_malformed_jwt_two_segments(self, client):
        """Token with only 2 segments returns 401."""
        resp = await client.get("/api/protected", headers={"Authorization": "Bearer header.payload"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_invalid_base64_payload(self, client):
        """Token with invalid base64 in payload returns 401."""
        resp = await client.get("/api/protected", headers={"Authorization": "Bearer header.!!!invalid!!!.sig"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_non_json_payload(self, client):
        """Token with non-JSON payload returns 401."""
        payload_b64 = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
        token = f"header.{payload_b64}.sig"
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_non_dict_json_payload_returns_401(self, client):
        """Token with valid JSON but non-dict payload (array) returns 401, not 500."""
        payload_b64 = base64.urlsafe_b64encode(json.dumps([1, 2, 3]).encode()).decode().rstrip("=")
        header_b64 = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
        token = f"{header_b64}.{payload_b64}.sig"
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_string_json_payload_returns_401(self, client):
        """Token with JSON string payload returns 401, not 500."""
        payload_b64 = base64.urlsafe_b64encode(json.dumps("hello").encode()).decode().rstrip("=")
        header_b64 = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
        token = f"{header_b64}.{payload_b64}.sig"
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401


class TestDefenseInDepth:
    """Defense-in-depth claim validation for the 'Tyk silently not in front' case.

    Closes the gap exposed by issue #240, where tyk/entrypoint.sh was
    silently broken for four days and nobody caught it. If gateway mode
    is configured but Tyk is not actually validating tokens, these checks
    prevent forged or expired payloads from being trusted.
    """

    @pytest.mark.anyio
    async def test_missing_exp_returns_401(self, client):
        """JWT without an ``exp`` claim is rejected."""
        token = _make_token({"sub": "user1", "dct": "t1", "tenants": {"t1": {}}}, auto_exp=False)
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_expired_exp_returns_401(self, client):
        """JWT with ``exp`` in the past (beyond leeway) is rejected."""
        token = _make_token({"sub": "user1", "dct": "t1", "tenants": {"t1": {}}, "exp": int(time.time()) - 3600})
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_string_exp_returns_401(self, client):
        """JWT with a non-numeric ``exp`` value is rejected."""
        token = _make_token({"sub": "user1", "dct": "t1", "tenants": {"t1": {}}, "exp": "never"})
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_bool_exp_returns_401(self, client):
        """JWT with a bool ``exp`` value is rejected (bool is a subclass of int)."""
        token = _make_token({"sub": "user1", "dct": "t1", "tenants": {"t1": {}}, "exp": True})
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_exp_within_leeway_accepted(self, client):
        """JWT with ``exp`` just past current time but inside the 30s leeway is accepted."""
        token = _make_token({"sub": "user1", "dct": "t1", "tenants": {"t1": {}}, "exp": int(time.time()) - 10})
        resp = await client.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_missing_iss_with_project_id_returns_401(self):
        """With descope_project_id configured, JWT missing ``iss`` is rejected."""
        app = _build_app(descope_project_id="P123")
        token = _make_token({"sub": "user1", "dct": "t1", "tenants": {"t1": {}}})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_wrong_iss_with_project_id_returns_401(self):
        """With descope_project_id configured, JWT with unknown ``iss`` is rejected."""
        app = _build_app(descope_project_id="P123")
        token = _make_token(
            {
                "sub": "user1",
                "dct": "t1",
                "tenants": {"t1": {}},
                "iss": "https://api.descope.com/P456",  # wrong project id
            }
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_non_string_iss_with_project_id_returns_401(self):
        """With descope_project_id configured, JWT with non-string ``iss`` is rejected."""
        app = _build_app(descope_project_id="P123")
        token = _make_token({"sub": "user1", "dct": "t1", "tenants": {"t1": {}}, "iss": 42})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_oidc_iss_accepted(self):
        """OIDC-format issuer https://api.descope.com/<project> is accepted."""
        app = _build_app(descope_project_id="P123")
        token = _make_token(
            {
                "sub": "user1",
                "dct": "t1",
                "tenants": {"t1": {}},
                "iss": "https://api.descope.com/P123",
            }
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_session_token_iss_accepted(self):
        """Session-token issuer https://api.descope.com/v1/apps/<project> is accepted."""
        app = _build_app(descope_project_id="P123")
        token = _make_token(
            {
                "sub": "user1",
                "dct": "t1",
                "tenants": {"t1": {}},
                "iss": "https://api.descope.com/v1/apps/P123",
            }
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_aud_mismatch_returns_401(self):
        """With descope_project_id set and an ``aud`` present, a mismatch is rejected."""
        app = _build_app(descope_project_id="P123")
        token = _make_token(
            {
                "sub": "user1",
                "dct": "t1",
                "tenants": {"t1": {}},
                "iss": "https://api.descope.com/P123",
                "aud": "P456",  # wrong project id
            }
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_aud_list_containing_project_id_accepted(self):
        """With ``aud`` as a list containing the project id, the token is accepted."""
        app = _build_app(descope_project_id="P123")
        token = _make_token(
            {
                "sub": "user1",
                "dct": "t1",
                "tenants": {"t1": {}},
                "iss": "https://api.descope.com/P123",
                "aud": ["P123", "other"],
            }
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_missing_aud_with_project_id_accepted(self):
        """Descope session tokens omit aud entirely — that must still be accepted."""
        app = _build_app(descope_project_id="P123")
        token = _make_token(
            {
                "sub": "user1",
                "dct": "t1",
                "tenants": {"t1": {}},
                "iss": "https://api.descope.com/v1/apps/P123",
                # intentionally no aud
            }
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/protected", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200


class TestRBACIntegration:
    """AC-5: RBAC works through gateway-mode ASGI stack with GatewayClaimsMiddleware."""

    @pytest.mark.anyio
    async def test_insufficient_roles_returns_403(self):
        """JWT with wrong role gets 403 from require_role dependency."""
        app = FastAPI()

        @app.get("/api/admin", dependencies=[Depends(require_role("admin"))])
        async def admin_route():
            return JSONResponse({"ok": True})

        app.add_middleware(GatewayClaimsMiddleware, excluded_paths={"/api/health"})

        payload = {"sub": "user1", "dct": "t1", "tenants": {"t1": {"roles": ["viewer"]}}}
        token = _make_token(payload)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/admin", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_correct_role_returns_200(self):
        """JWT with correct role gets 200 through gateway-mode RBAC."""
        app = FastAPI()

        @app.get("/api/admin", dependencies=[Depends(require_role("admin"))])
        async def admin_route():
            return JSONResponse({"ok": True})

        app.add_middleware(GatewayClaimsMiddleware, excluded_paths={"/api/health"})

        payload = {"sub": "user1", "dct": "t1", "tenants": {"t1": {"roles": ["admin"]}}}
        token = _make_token(payload)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/admin", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200
