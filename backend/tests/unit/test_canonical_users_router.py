"""Unit tests for /api/users canonical user listing (DS-4.0)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_user_service
from app.main import app
from app.models.identity.user import User, UserStatus
from app.services.user import UserService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")


@pytest.fixture
def mock_user_service():
    return AsyncMock(spec=UserService)


@pytest.fixture(autouse=True)
def _override_service(mock_user_service):
    app.dependency_overrides[get_user_service] = lambda: mock_user_service
    yield
    app.dependency_overrides.pop(get_user_service, None)


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


def _make_user(status: UserStatus = UserStatus.active) -> User:
    now = datetime.now(timezone.utc)
    return User(
        id=uuid.uuid4(),
        email=f"user-{uuid.uuid4().hex[:6]}@example.com",
        user_name="testuser",
        given_name="Test",
        family_name="User",
        status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.anyio
async def test_list_users_requires_auth(client):
    response = await client.get("/api/users")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_users_rejects_admin(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.get("/api/users", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_users_returns_payload(mock_validate, mock_user_service, client):
    """Router serializes each User domain object to the public payload shape verbatim."""
    mock_validate.return_value = OPERATOR_CLAIMS
    users = [_make_user(), _make_user()]
    mock_user_service.list_canonical_users.return_value = users

    response = await client.get("/api/users", headers=AUTH_HEADER)

    assert response.status_code == 200
    expected_payload = {
        "users": [
            {
                "id": str(u.id),
                "email": u.email,
                "user_name": u.user_name,
                "given_name": u.given_name,
                "family_name": u.family_name,
                "status": u.status.value,
                "created_at": u.created_at.isoformat(),
                "updated_at": u.updated_at.isoformat(),
            }
            for u in users
        ]
    }
    assert response.json() == expected_payload


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_status_provisional_maps_to_provisioned(mock_validate, mock_user_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    mock_user_service.list_canonical_users.return_value = [_make_user(UserStatus.provisioned)]

    response = await client.get("/api/users?status=provisional", headers=AUTH_HEADER)
    assert response.status_code == 200
    kwargs = mock_user_service.list_canonical_users.await_args.kwargs
    assert kwargs["status"] == UserStatus.provisioned


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_status_canonical_value_passes_through(mock_validate, mock_user_service, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    mock_user_service.list_canonical_users.return_value = []

    response = await client.get("/api/users?status=inactive", headers=AUTH_HEADER)
    assert response.status_code == 200
    kwargs = mock_user_service.list_canonical_users.await_args.kwargs
    assert kwargs["status"] == UserStatus.inactive


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invalid_status_returns_422(mock_validate, client):
    mock_validate.return_value = OPERATOR_CLAIMS
    response = await client.get("/api/users?status=banana", headers=AUTH_HEADER)
    assert response.status_code == 422


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_limit_validation(mock_validate, client):
    mock_validate.return_value = OPERATOR_CLAIMS

    response = await client.get("/api/users?limit=0", headers=AUTH_HEADER)
    assert response.status_code == 422

    response = await client.get("/api/users?limit=10000", headers=AUTH_HEADER)
    assert response.status_code == 422
