"""Integration tests for /api/users canonical user listing (DS-4.0).

Exercises operator-only auth tiers, status filter resolution including the
`provisional` alias, limit validation, and rejection of unknown status —
all against real Postgres seeded with users in each canonical status.
"""

import os

os.environ.setdefault("DESCOPE_PROJECT_ID", "test-project-id")
os.environ.setdefault("DESCOPE_MANAGEMENT_KEY", "test-management-key")

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.models.database import get_async_session
from app.models.identity.user import User, UserStatus

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
    """Yield (app, client) with get_async_session pinned to the integration db_session."""
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
async def seeded_users(db_session):
    """Seed at least one user in each canonical UserStatus for filter checks."""
    suffix = uuid.uuid4().hex[:8]
    rows = {
        UserStatus.active: User(
            email=f"active-{suffix}@example.com",
            user_name=f"active-{suffix}",
            status=UserStatus.active,
        ),
        UserStatus.inactive: User(
            email=f"inactive-{suffix}@example.com",
            user_name=f"inactive-{suffix}",
            status=UserStatus.inactive,
        ),
        UserStatus.provisioned: User(
            email=f"provisioned-{suffix}@example.com",
            user_name=f"provisioned-{suffix}",
            status=UserStatus.provisioned,
        ),
    }
    for u in rows.values():
        db_session.add(u)
    await db_session.flush()
    return rows


# ──────────────────────────────────────────────────────────────────────
# Auth tier enforcement
# ──────────────────────────────────────────────────────────────────────


async def test_list_users_unauthenticated_rejected(app_with_session):
    _, client = app_with_session
    resp = await client.get("/api/users")
    assert resp.status_code == 401


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_users_admin_role_forbidden(mock_validate, app_with_session):
    mock_validate.return_value = ADMIN_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/users", headers=AUTH_HEADER)
    assert resp.status_code == 403


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_users_service_account_without_operator_role_forbidden(mock_validate, app_with_session):
    mock_validate.return_value = SERVICE_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/users", headers=AUTH_HEADER)
    assert resp.status_code == 403


# ──────────────────────────────────────────────────────────────────────
# Status filter — each enum value, alias resolution, and rejection
# ──────────────────────────────────────────────────────────────────────


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
@pytest.mark.parametrize(
    "status_value, expected_status",
    [
        ("active", UserStatus.active),
        ("inactive", UserStatus.inactive),
        ("provisioned", UserStatus.provisioned),
    ],
)
async def test_list_users_filters_by_each_status(
    mock_validate, app_with_session, seeded_users, status_value, expected_status
):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get(f"/api/users?status={status_value}", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    users = resp.json()["users"]
    expected_user = seeded_users[expected_status]
    matched = [u for u in users if u["id"] == str(expected_user.id)]
    assert len(matched) == 1
    assert matched[0]["status"] == status_value
    # No row of any other status should leak through
    assert all(u["status"] == status_value for u in users)


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_users_provisional_alias_resolves_to_provisioned(mock_validate, app_with_session, seeded_users):
    """`?status=provisional` is the spec/UX term and must filter to UserStatus.provisioned."""
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/users?status=provisional", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    users = resp.json()["users"]
    assert all(u["status"] == "provisioned" for u in users)
    expected = seeded_users[UserStatus.provisioned]
    assert any(u["id"] == str(expected.id) for u in users)


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_users_unknown_status_returns_422(mock_validate, app_with_session):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/users?status=banana", headers=AUTH_HEADER)
    assert resp.status_code == 422


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_users_no_status_filter_returns_all(mock_validate, app_with_session, seeded_users):
    """Omitting status returns rows of multiple statuses (no filtering)."""
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/users", headers=AUTH_HEADER)
    assert resp.status_code == 200
    users = resp.json()["users"]
    seeded_ids = {str(u.id) for u in seeded_users.values()}
    returned_ids = {u["id"] for u in users}
    assert seeded_ids <= returned_ids


# ──────────────────────────────────────────────────────────────────────
# Limit validation
# ──────────────────────────────────────────────────────────────────────


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
@pytest.mark.parametrize("limit", [1, 50, 200])
async def test_list_users_accepts_valid_limits(mock_validate, app_with_session, limit):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get(f"/api/users?limit={limit}", headers=AUTH_HEADER)
    assert resp.status_code == 200, f"limit={limit} rejected: {resp.text}"


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
@pytest.mark.parametrize("limit", [0, 201, -1, 10000])
async def test_list_users_rejects_invalid_limits(mock_validate, app_with_session, limit):
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get(f"/api/users?limit={limit}", headers=AUTH_HEADER)
    assert resp.status_code == 422


@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_users_payload_shape(mock_validate, app_with_session, seeded_users):
    """Returned rows include the documented serialisation fields."""
    mock_validate.return_value = OPERATOR_CLAIMS
    _, client = app_with_session
    resp = await client.get("/api/users", headers=AUTH_HEADER)
    assert resp.status_code == 200
    users = resp.json()["users"]
    assert users, "expected seeded users in response"
    keys = set(users[0].keys())
    assert {"id", "email", "user_name", "given_name", "family_name", "status", "created_at", "updated_at"} <= keys
