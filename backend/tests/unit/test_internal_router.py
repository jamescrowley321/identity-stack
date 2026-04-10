"""Unit tests for the internal router (Story 3.1 + Story 4.3).

Tests cover:
- POST /api/internal/users/sync — flow connector endpoint (with shared secret)
- POST /api/internal/webhooks/descope — webhook endpoint with HMAC
- AC-3.1.3: HMAC validation (valid, invalid, missing secret)
- AC-3.1.4: Internal endpoints bypass JWT auth
- Flow sync shared secret validation
- Error handling via result_to_response
- AC-4.3.1: GET /api/internal/identity — identity resolution endpoint
- AC-4.3.4: Identity endpoint bypasses JWT auth
"""

import hashlib
import hmac as hmac_mod
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_identity_resolution_service, get_inbound_sync_service
from app.errors.identity import Conflict, NotFound, ValidationError
from app.main import app
from app.services.identity_resolution import IdentityResolutionService
from app.services.inbound_sync import InboundSyncService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_sync_service():
    return AsyncMock(spec=InboundSyncService)


@pytest.fixture(autouse=True)
def _override_services(mock_sync_service):
    app.dependency_overrides[get_inbound_sync_service] = lambda: mock_sync_service
    yield
    app.dependency_overrides.pop(get_inbound_sync_service, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


SAMPLE_USER_DICT = {
    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "email": "alice@example.com",
    "user_name": "alice@example.com",
    "given_name": "Alice",
    "family_name": "Smith",
    "status": "active",
}

WEBHOOK_SECRET = "test-webhook-secret-key"
FLOW_SECRET = "test-flow-sync-secret"
IDENTITY_KEY = "test-identity-key"


def _compute_hmac(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- AC-3.1.4: Internal endpoints bypass JWT auth ---


@pytest.mark.anyio
@patch.dict("os.environ", {"DESCOPE_FLOW_SYNC_SECRET": FLOW_SECRET})
async def test_flow_sync_no_jwt_required(mock_sync_service, client):
    """Internal endpoints excluded from JWT — needs X-Flow-Secret, not Authorization."""
    # Reload module-level secret
    import app.routers.internal as mod

    mod._FLOW_SYNC_SECRET = FLOW_SECRET
    try:
        mock_sync_service.sync_user_from_flow.return_value = Ok({"user": SAMPLE_USER_DICT, "created": True})

        response = await client.post(
            "/api/internal/users/sync",
            json={"user_id": "ext-1", "email": "alice@example.com"},
            headers={"X-Flow-Secret": FLOW_SECRET},
        )

        # Should NOT get 401 from JWT middleware — internal endpoints bypass JWT auth
        assert response.status_code == 201
        mock_sync_service.sync_user_from_flow.assert_awaited_once()
    finally:
        mod._FLOW_SYNC_SECRET = ""


# --- Flow sync shared secret validation ---


@pytest.mark.anyio
async def test_flow_sync_missing_secret_header_returns_422(client):
    """Missing X-Flow-Secret header → 422 (FastAPI header validation)."""
    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1", "email": "alice@example.com"},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_flow_sync_invalid_secret_returns_401(client):
    """Invalid X-Flow-Secret → 401."""
    import app.routers.internal as mod

    mod._FLOW_SYNC_SECRET = FLOW_SECRET
    try:
        response = await client.post(
            "/api/internal/users/sync",
            json={"user_id": "ext-1", "email": "alice@example.com"},
            headers={"X-Flow-Secret": "wrong-secret"},
        )

        assert response.status_code == 401
    finally:
        mod._FLOW_SYNC_SECRET = ""


@pytest.mark.anyio
async def test_flow_sync_unconfigured_secret_returns_401(client):
    """DESCOPE_FLOW_SYNC_SECRET empty → 401."""
    import app.routers.internal as mod

    mod._FLOW_SYNC_SECRET = ""

    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1", "email": "alice@example.com"},
        headers={"X-Flow-Secret": "any-value"},
    )

    assert response.status_code == 401


# --- AC-3.1.1: Flow sync endpoint ---


def _flow_sync_headers():
    return {"X-Flow-Secret": FLOW_SECRET}


@pytest.fixture(autouse=True)
def _set_flow_secret():
    import app.routers.internal as mod

    mod._FLOW_SYNC_SECRET = FLOW_SECRET
    yield
    mod._FLOW_SYNC_SECRET = ""


@pytest.mark.anyio
async def test_flow_sync_new_user_returns_201(mock_sync_service, client):
    mock_sync_service.sync_user_from_flow.return_value = Ok({"user": SAMPLE_USER_DICT, "created": True})

    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1", "email": "alice@example.com", "name": "Alice Smith"},
        headers=_flow_sync_headers(),
    )

    assert response.status_code == 201
    data = response.json()
    assert data["user"]["email"] == "alice@example.com"
    assert data["created"] is True


@pytest.mark.anyio
async def test_flow_sync_existing_user_returns_200(mock_sync_service, client):
    mock_sync_service.sync_user_from_flow.return_value = Ok({"user": SAMPLE_USER_DICT, "created": False})

    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1", "email": "alice@example.com"},
        headers=_flow_sync_headers(),
    )

    assert response.status_code == 200


@pytest.mark.anyio
async def test_flow_sync_conflict_returns_409(mock_sync_service, client):
    mock_sync_service.sync_user_from_flow.return_value = Error(Conflict(message="Email already exists"))

    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1", "email": "dup@example.com"},
        headers=_flow_sync_headers(),
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.anyio
async def test_flow_sync_not_found_returns_404(mock_sync_service, client):
    """Provider not configured → NotFound."""
    mock_sync_service.sync_user_from_flow.return_value = Error(NotFound(message="Descope provider not configured"))

    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1", "email": "a@b.com"},
        headers=_flow_sync_headers(),
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_flow_sync_validation_error_returns_422(mock_sync_service, client):
    mock_sync_service.sync_user_from_flow.return_value = Error(ValidationError(message="Email is required"))

    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1", "email": "a@b.com"},
        headers=_flow_sync_headers(),
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_flow_sync_missing_required_field_returns_422(client):
    """Pydantic validation: missing email → 422 before reaching service."""
    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1"},
        headers=_flow_sync_headers(),
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_flow_sync_invalid_email_returns_422(client):
    """Pydantic EmailStr validation: malformed email → 422."""
    response = await client.post(
        "/api/internal/users/sync",
        json={"user_id": "ext-1", "email": "not-an-email"},
        headers=_flow_sync_headers(),
    )

    assert response.status_code == 422


# --- AC-3.1.2: Webhook endpoint ---


@pytest.mark.anyio
async def test_webhook_valid_hmac(mock_sync_service, client):
    """Valid HMAC signature → request processed."""
    import app.routers.internal as mod

    mod._WEBHOOK_SECRET = WEBHOOK_SECRET
    try:
        mock_sync_service.process_webhook_event.return_value = Ok({"status": "ignored", "event_type": "tenant.created"})

        import json

        body_bytes = json.dumps({"event_type": "tenant.created", "data": {}}).encode()
        sig = _compute_hmac(body_bytes)

        response = await client.post(
            "/api/internal/webhooks/descope",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Descope-Webhook-S256": sig,
            },
        )

        assert response.status_code == 200
        mock_sync_service.process_webhook_event.assert_awaited_once()
    finally:
        mod._WEBHOOK_SECRET = ""


# --- AC-3.1.3: HMAC validation ---


@pytest.mark.anyio
async def test_webhook_invalid_hmac_returns_401(client):
    """Invalid HMAC signature → 401."""
    import app.routers.internal as mod

    mod._WEBHOOK_SECRET = WEBHOOK_SECRET
    try:
        response = await client.post(
            "/api/internal/webhooks/descope",
            json={"event_type": "user.created", "data": {}},
            headers={"X-Descope-Webhook-S256": "badbadbadbad"},
        )

        assert response.status_code == 401
    finally:
        mod._WEBHOOK_SECRET = ""


@pytest.mark.anyio
async def test_webhook_missing_hmac_header_returns_422(client):
    """Missing X-Descope-Webhook-S256 header → 422 (FastAPI header validation)."""
    response = await client.post(
        "/api/internal/webhooks/descope",
        json={"event_type": "user.created", "data": {}},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_webhook_missing_secret_returns_401(client):
    """DESCOPE_WEBHOOK_SECRET empty → 401 (secret not configured)."""
    import app.routers.internal as mod

    mod._WEBHOOK_SECRET = ""

    response = await client.post(
        "/api/internal/webhooks/descope",
        json={"event_type": "user.created", "data": {}},
        headers={"X-Descope-Webhook-S256": "anysig"},
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_webhook_no_auth_required(mock_sync_service, client):
    """Webhook endpoint also bypasses JWT auth (internal prefix)."""
    # Even without secret env, the webhook handler should not return 401 from JWT
    # It returns 422 because the HMAC header is missing (not 401 from JWT)
    response = await client.post(
        "/api/internal/webhooks/descope",
        json={"event_type": "user.created", "data": {}},
    )

    # 422 from missing header, NOT 401 from JWT middleware
    assert response.status_code == 422


# --- AC-4.3.1 / AC-4.3.4: Identity resolution endpoint ---


@pytest.fixture
def mock_resolution_service():
    return AsyncMock(spec=IdentityResolutionService)


@pytest.fixture(autouse=True)
def _override_resolution_service(mock_resolution_service):
    app.dependency_overrides[get_identity_resolution_service] = lambda: mock_resolution_service
    yield
    app.dependency_overrides.pop(get_identity_resolution_service, None)


@pytest.fixture(autouse=True)
def _set_identity_key(monkeypatch):
    """Set the INTERNAL_IDENTITY_KEY env var and patch the module-level variable."""
    monkeypatch.setenv("INTERNAL_IDENTITY_KEY", IDENTITY_KEY)
    import app.routers.internal as mod

    monkeypatch.setattr(mod, "_IDENTITY_KEY", IDENTITY_KEY)


SAMPLE_IDENTITY_PAYLOAD = {
    "user": {
        "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "email": "alice@example.com",
        "user_name": "alice",
        "given_name": "Alice",
        "family_name": "Smith",
        "status": "active",
    },
    "roles": [{"tenant_id": "t-1", "role_name": "admin", "permissions": ["read:users"]}],
    "tenant_memberships": [{"tenant_id": "t-1", "tenant_name": "Acme"}],
    "linked_idps": [{"provider_name": "descope", "external_sub": "ext-123"}],
}


@pytest.mark.anyio
async def test_identity_resolution_success(mock_resolution_service, client):
    """AC-4.3.1: GET /api/internal/identity returns full identity payload."""
    mock_resolution_service.resolve.return_value = Ok(SAMPLE_IDENTITY_PAYLOAD)

    response = await client.get(
        "/api/internal/identity",
        params={"sub": "ext-123", "provider": "descope"},
        headers={"X-Identity-Key": IDENTITY_KEY},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["user"]["email"] == "alice@example.com"
    assert len(data["roles"]) == 1
    assert data["roles"][0]["role_name"] == "admin"
    mock_resolution_service.resolve.assert_awaited_once_with(provider="descope", sub="ext-123")


@pytest.mark.anyio
async def test_identity_resolution_provider_not_found(mock_resolution_service, client):
    """Unknown provider returns 404."""
    mock_resolution_service.resolve.return_value = Error(NotFound(message="Provider 'unknown' not found"))

    response = await client.get(
        "/api/internal/identity",
        params={"sub": "ext-123", "provider": "unknown"},
        headers={"X-Identity-Key": IDENTITY_KEY},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_identity_resolution_link_not_found(mock_resolution_service, client):
    """No IdP link for sub+provider returns 404."""
    mock_resolution_service.resolve.return_value = Error(
        NotFound(message="No identity found for provider 'descope' with sub 'nonexistent'")
    )

    response = await client.get(
        "/api/internal/identity",
        params={"sub": "nonexistent", "provider": "descope"},
        headers={"X-Identity-Key": IDENTITY_KEY},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_identity_resolution_missing_sub_param(client):
    """Missing 'sub' query parameter → 422."""
    response = await client.get(
        "/api/internal/identity",
        params={"provider": "descope"},
        headers={"X-Identity-Key": IDENTITY_KEY},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_identity_resolution_missing_provider_param(client):
    """Missing 'provider' query parameter → 422."""
    response = await client.get(
        "/api/internal/identity",
        params={"sub": "ext-123"},
        headers={"X-Identity-Key": IDENTITY_KEY},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_identity_resolution_missing_both_params(client):
    """Missing both query parameters → 422."""
    response = await client.get(
        "/api/internal/identity",
        headers={"X-Identity-Key": IDENTITY_KEY},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_identity_missing_key_returns_422(client):
    """Missing X-Identity-Key header → 422."""
    response = await client.get(
        "/api/internal/identity",
        params={"sub": "ext-123", "provider": "descope"},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_identity_invalid_key_returns_401(client):
    """Invalid X-Identity-Key → 401."""
    response = await client.get(
        "/api/internal/identity",
        params={"sub": "ext-123", "provider": "descope"},
        headers={"X-Identity-Key": "wrong-key"},
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_identity_unconfigured_key_returns_401(client, monkeypatch):
    """Unconfigured INTERNAL_IDENTITY_KEY → 401."""
    import app.routers.internal as mod

    monkeypatch.setattr(mod, "_IDENTITY_KEY", "")

    response = await client.get(
        "/api/internal/identity",
        params={"sub": "ext-123", "provider": "descope"},
        headers={"X-Identity-Key": "any-key"},
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_identity_endpoint_bypasses_jwt(client):
    """AC-4.3.4: /api/internal/identity bypasses JWT auth.

    Without Authorization header but with valid identity key, does NOT return 401 from JWT middleware.
    Returns 422 (missing params) which proves JWT was bypassed.
    """
    response = await client.get(
        "/api/internal/identity",
        headers={"X-Identity-Key": IDENTITY_KEY},
    )

    # 422 from missing query params, NOT 401 from JWT middleware
    assert response.status_code == 422
