"""Unit tests for /api/sync/status and /api/events/recent (DS-4.0)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from expression import Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_sync_status_service
from app.main import app
from app.services.sync_status import SyncStatusService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_service():
    return AsyncMock(spec=SyncStatusService)


@pytest.fixture(autouse=True)
def _override_service(mock_service):
    app.dependency_overrides[get_sync_status_service] = lambda: mock_service
    yield
    app.dependency_overrides.pop(get_sync_status_service, None)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


TENANT_ID = "51c5957b-684a-453f-8ab1-8f239999c4d8"
OPERATOR_CLAIMS = {
    "sub": "operator1",
    "dct": TENANT_ID,
    "tenants": {TENANT_ID: {"roles": ["operator"], "permissions": []}},
}
ADMIN_CLAIMS = {
    "sub": "admin1",
    "dct": TENANT_ID,
    "tenants": {TENANT_ID: {"roles": ["admin"], "permissions": []}},
}
AUTH_HEADER = {"Authorization": "Bearer valid.token"}


# --- Auth enforcement ---


@pytest.mark.anyio
async def test_sync_status_requires_auth(client):
    response = await client.get("/api/sync/status")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_events_recent_requires_auth(client):
    response = await client.get("/api/events/recent")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_sync_status_rejects_admin(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/sync/status", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_rejects_admin(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/events/recent", headers=AUTH_HEADER)
    assert response.status_code == 403


# --- Happy paths ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_sync_status_returns_aggregated_payload(mock_validate, mock_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    mock_service.get_status.return_value = Ok(
        {
            "providers": [
                {
                    "id": str(uuid.uuid4()),
                    "name": "descope-prod",
                    "type": "descope",
                    "status": "active",
                    "user_count": 12,
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                }
            ],
            "last_reconciliation": datetime.now(timezone.utc).isoformat(),
        }
    )

    response = await client.get("/api/sync/status", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "providers" in data
    assert "last_reconciliation" in data
    assert data["providers"][0]["user_count"] == 12


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_returns_event_list(mock_validate, mock_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    mock_service.list_events.return_value = Ok(
        {
            "events": [
                {
                    "id": str(uuid.uuid4()),
                    "provider_id": str(uuid.uuid4()),
                    "verb": "created",
                    "subject_type": "user",
                    "subject_id": str(uuid.uuid4()),
                    "external_sub": "ext-1",
                    "detail": None,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
    )

    response = await client.get("/api/events/recent?limit=10", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert len(data["events"]) == 1
    mock_service.list_events.assert_awaited_once()
    kwargs = mock_service.list_events.await_args.kwargs
    assert kwargs["limit"] == 10
    assert kwargs["provider_id"] is None
    assert kwargs["verb"] is None


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_passes_filters(mock_validate, mock_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    mock_service.list_events.return_value = Ok({"events": []})
    pid = str(uuid.uuid4())

    response = await client.get(
        f"/api/events/recent?limit=20&provider={pid}&verb=created",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    kwargs = mock_service.list_events.await_args.kwargs
    assert kwargs["limit"] == 20
    assert str(kwargs["provider_id"]) == pid
    assert kwargs["verb"].value == "created"


# --- Validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_rejects_invalid_provider(mock_validate, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    response = await client.get("/api/events/recent?provider=not-a-uuid", headers=AUTH_HEADER)
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_rejects_invalid_verb(mock_validate, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    response = await client.get("/api/events/recent?verb=banana", headers=AUTH_HEADER)
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_rejects_invalid_limit(mock_validate, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    response = await client.get("/api/events/recent?limit=0", headers=AUTH_HEADER)
    assert response.status_code == 422

    response = await client.get("/api/events/recent?limit=10000", headers=AUTH_HEADER)
    assert response.status_code == 422
