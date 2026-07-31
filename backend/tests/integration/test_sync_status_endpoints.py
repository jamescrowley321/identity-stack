"""Integration tests for /api/sync/status and /api/events/recent (DS-4.0).

Real HTTP via httpx ASGITransport, real Postgres via the integration db_session,
and validate_token patched to inject the desired claim shape. Operator-only
tier enforcement is checked alongside aggregation correctness against seeded
identity tables.
"""

import os

os.environ.setdefault("DESCOPE_PROJECT_ID", "test-project-id")
os.environ.setdefault("DESCOPE_MANAGEMENT_KEY", "test-management-key")

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.models.database import get_async_session
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.models.identity.user import IdPLink, User, UserStatus

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
SERVICE_CLAIMS = {
    "sub": "service-account-1",
    "dct": TENANT_ID,
    "tenants": {TENANT_ID: {"roles": [], "permissions": []}},
}
AUTH_HEADER = {"Authorization": "Bearer valid.token"}


@pytest_asyncio.fixture(loop_scope="session")
async def app_with_session(db_session):
    """Yield (app, client) with get_async_session overridden to the test db_session.

    Also seeds app.state with stub Descope/cache clients so dependency
    factories that touch app.state don't AttributeError.
    """
    from app.main import app

    async def _yield_db():
        yield db_session

    app.dependency_overrides[get_async_session] = _yield_db
    prior_descope = getattr(app.state, "descope_client", None)
    prior_publisher = getattr(app.state, "cache_publisher", None)
    app.state.descope_client = AsyncMock()
    app.state.cache_publisher = AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield app, client

    app.dependency_overrides.pop(get_async_session, None)
    app.state.descope_client = prior_descope
    app.state.cache_publisher = prior_publisher


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(db_session):
    """Seed two providers + idp_links + sync_events used by the endpoint tests."""
    suffix = uuid.uuid4().hex[:8]
    base = datetime.now(timezone.utc)

    active = Provider(name=f"descope-prod-{suffix}", type=ProviderType.descope, active=True)
    inactive = Provider(name=f"oidc-disabled-{suffix}", type=ProviderType.oidc, active=False)
    db_session.add(active)
    db_session.add(inactive)
    await db_session.flush()

    users = [
        User(email=f"u-{i}-{suffix}@example.com", user_name=f"u-{i}-{suffix}", status=UserStatus.active)
        for i in range(3)
    ]
    for u in users:
        db_session.add(u)
    await db_session.flush()

    db_session.add(IdPLink(user_id=users[0].id, provider_id=active.id, external_sub=f"e-0-{suffix}"))
    db_session.add(IdPLink(user_id=users[1].id, provider_id=active.id, external_sub=f"e-1-{suffix}"))
    db_session.add(IdPLink(user_id=users[2].id, provider_id=inactive.id, external_sub=f"e-2-{suffix}"))
    await db_session.flush()

    events = []
    for offset_minutes, verb, provider in [
        (120, SyncEventVerb.created, active),
        (60, SyncEventVerb.linked, inactive),
        (10, SyncEventVerb.updated, active),
    ]:
        e = SyncEvent(
            provider_id=provider.id,
            verb=verb,
            subject_type="user",
            subject_id=str(uuid.uuid4()),
            external_sub=f"sub-{uuid.uuid4().hex[:6]}",
        )
        e.occurred_at = base - timedelta(minutes=offset_minutes)
        db_session.add(e)
        events.append(e)
    await db_session.flush()

    return {
        "active": active,
        "inactive": inactive,
        "users": users,
        "events": events,
        "newest_event": events[2],  # 10-min-old "updated" on active
    }


# ──────────────────────────────────────────────────────────────────────
# Auth tier enforcement
# ──────────────────────────────────────────────────────────────────────


async def test_sync_status_unauthenticated_rejected(app_with_session):
    _, client = app_with_session
    resp = await client.get("/api/sync/status")
    assert resp.status_code == 401


async def test_events_recent_unauthenticated_rejected(app_with_session):
    _, client = app_with_session
    resp = await client.get("/api/events/recent")
    assert resp.status_code == 401


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_sync_status_admin_role_forbidden(mock_validate, app_with_session):
    """Admin (non-operator) tier rejected with 403 — operator-only endpoint."""
    mock_validate.return_value = ADMIN_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/sync/status", headers=AUTH_HEADER)
    assert resp.status_code == 403


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_admin_role_forbidden(mock_validate, app_with_session):
    mock_validate.return_value = ADMIN_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/events/recent", headers=AUTH_HEADER)
    assert resp.status_code == 403


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_sync_status_service_account_without_operator_role_forbidden(mock_validate, app_with_session):
    """Service-to-service (OIDC client credentials) without operator role still rejected."""
    mock_validate.return_value = SERVICE_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/sync/status", headers=AUTH_HEADER)
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────
# /api/sync/status — happy paths against real DB
# ──────────────────────────────────────────────────────────────────────


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_sync_status_aggregates_against_real_db(mock_validate, app_with_session, seeded):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/sync/status", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "providers" in body
    assert "last_reconciliation" in body

    by_id = {p["id"]: p for p in body["providers"]}
    active = seeded["active"]
    inactive = seeded["inactive"]
    assert str(active.id) in by_id
    assert str(inactive.id) in by_id

    assert by_id[str(active.id)]["status"] == "active"
    assert by_id[str(active.id)]["user_count"] == 2
    assert by_id[str(inactive.id)]["status"] == "inactive"
    assert by_id[str(inactive.id)]["user_count"] == 1

    expected_last = seeded["newest_event"].occurred_at.isoformat()
    assert body["last_reconciliation"] == expected_last
    assert by_id[str(active.id)]["last_sync"] == expected_last


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_sync_status_empty_state(mock_validate, app_with_session):
    """No providers and no events → empty list and null last_reconciliation."""
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/sync/status", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["providers"] == []
    assert body["last_reconciliation"] is None


# ──────────────────────────────────────────────────────────────────────
# /api/events/recent — limit + filter validation against real DB
# ──────────────────────────────────────────────────────────────────────


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_orders_desc_by_occurred_at(mock_validate, app_with_session, seeded):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/events/recent?limit=50", headers=AUTH_HEADER)
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) >= 3
    timestamps = [e["occurred_at"] for e in events]
    assert timestamps == sorted(timestamps, reverse=True)


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
@pytest.mark.parametrize("limit", [1, 50, 200])
async def test_events_recent_limit_accepted_values(mock_validate, app_with_session, limit):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get(f"/api/events/recent?limit={limit}", headers=AUTH_HEADER)
    assert resp.status_code == 200, f"limit={limit} rejected: {resp.text}"


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
@pytest.mark.parametrize("limit", [0, 201, -5, 9999])
async def test_events_recent_limit_rejected_values(mock_validate, app_with_session, limit):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get(f"/api/events/recent?limit={limit}", headers=AUTH_HEADER)
    assert resp.status_code == 422


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_filters_by_provider_uuid(mock_validate, app_with_session, seeded):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    inactive = seeded["inactive"]
    resp = await client.get(f"/api/events/recent?provider={inactive.id}", headers=AUTH_HEADER)
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["provider_id"] == str(inactive.id)


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_invalid_provider_uuid_returns_422(mock_validate, app_with_session):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/events/recent?provider=not-a-uuid", headers=AUTH_HEADER)
    assert resp.status_code == 422


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
@pytest.mark.parametrize(
    "verb",
    ["created", "updated", "deleted", "linked", "skipped", "failed"],
)
async def test_events_recent_filter_by_each_verb_value(mock_validate, app_with_session, seeded, verb):
    """Each enum value is accepted by the verb filter (no parse 422)."""
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get(f"/api/events/recent?verb={verb}", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    assert all(e["verb"] == verb for e in events)


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_events_recent_invalid_verb_returns_422(mock_validate, app_with_session):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/events/recent?verb=banana", headers=AUTH_HEADER)
    assert resp.status_code == 422
