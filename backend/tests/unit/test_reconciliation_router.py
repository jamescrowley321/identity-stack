"""Unit tests for the reconciliation router (Story 3.2).

Tests cover:
- POST /api/internal/reconciliation/run — trigger endpoint
- Flow sync secret validation (missing, invalid, unconfigured)
- Success response, ProviderError → 502, Conflict → 409
- Internal endpoint bypasses JWT auth
"""

from unittest.mock import AsyncMock

import pytest
from expression import Error, Ok
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_reconciliation_service
from app.errors.identity import Conflict, ProviderError
from app.main import app
from app.services.reconciliation import ReconciliationService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


FLOW_SECRET = "test-flow-sync-secret"


@pytest.fixture
def mock_recon_service():
    return AsyncMock(spec=ReconciliationService)


@pytest.fixture(autouse=True)
def _override_services(mock_recon_service):
    app.dependency_overrides[get_reconciliation_service] = lambda: mock_recon_service
    yield
    app.dependency_overrides.pop(get_reconciliation_service, None)


@pytest.fixture(autouse=True)
def _set_flow_secret():
    import app.routers.internal as mod

    mod._FLOW_SYNC_SECRET = FLOW_SECRET
    yield
    mod._FLOW_SYNC_SECRET = ""


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _headers():
    return {"X-Flow-Secret": FLOW_SECRET}


# --- Auth enforcement ---


@pytest.mark.anyio
async def test_reconciliation_no_jwt_required(mock_recon_service, client):
    """Internal endpoint bypasses JWT auth — needs X-Flow-Secret, not Authorization."""
    mock_recon_service.run.return_value = Ok({"status": "completed", "stats": {}})

    response = await client.post(
        "/api/internal/reconciliation/run",
        headers=_headers(),
    )

    assert response.status_code == 200
    mock_recon_service.run.assert_awaited_once()


@pytest.mark.anyio
async def test_reconciliation_missing_secret_header_returns_422(client):
    """Missing X-Flow-Secret header → 422 (FastAPI header validation)."""
    response = await client.post("/api/internal/reconciliation/run")

    assert response.status_code == 422


@pytest.mark.anyio
async def test_reconciliation_invalid_secret_returns_401(client):
    """Invalid X-Flow-Secret → 401."""
    response = await client.post(
        "/api/internal/reconciliation/run",
        headers={"X-Flow-Secret": "wrong-secret"},
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_reconciliation_unconfigured_secret_returns_401(client):
    """DESCOPE_FLOW_SYNC_SECRET empty → 401."""
    import app.routers.internal as mod

    mod._FLOW_SYNC_SECRET = ""

    response = await client.post(
        "/api/internal/reconciliation/run",
        headers={"X-Flow-Secret": "any-value"},
    )

    assert response.status_code == 401


# --- Success ---


@pytest.mark.anyio
async def test_reconciliation_success_returns_stats(mock_recon_service, client):
    stats = {
        "tenants_created": 1,
        "tenants_updated": 0,
        "permissions_created": 2,
        "permissions_updated": 0,
        "roles_created": 1,
        "roles_updated": 0,
        "users_created": 3,
        "users_updated": 1,
        "links_created": 3,
    }
    mock_recon_service.run.return_value = Ok({"status": "completed", "stats": stats})

    response = await client.post(
        "/api/internal/reconciliation/run",
        headers=_headers(),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["stats"]["users_created"] == 3


# --- Error responses ---


@pytest.mark.anyio
async def test_reconciliation_provider_error_returns_502(mock_recon_service, client):
    """Descope API unavailable → 502 Problem Detail."""
    mock_recon_service.run.return_value = Error(ProviderError(message="Descope API unavailable"))

    response = await client.post(
        "/api/internal/reconciliation/run",
        headers=_headers(),
    )

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.anyio
async def test_reconciliation_conflict_returns_409(mock_recon_service, client):
    """Reconciliation conflict → 409."""
    mock_recon_service.run.return_value = Error(Conflict(message="Lock contention"))

    response = await client.post(
        "/api/internal/reconciliation/run",
        headers=_headers(),
    )

    assert response.status_code == 409
