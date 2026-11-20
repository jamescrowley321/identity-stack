"""Unit tests for the users (member management) router.

The members router calls the Descope Management API directly via
request.app.state.descope_client -- no UserService / RoleService DI.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("DESCOPE_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "test-management-key")
    # Reset rate limiter storage between tests to avoid 429 interference
    from app.middleware.rate_limit import limiter

    limiter.reset()


@pytest.fixture
def mock_descope():
    """Return the Descope client mock already set on app.state by conftest."""
    return app.state.descope_client


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


TENANT_ID = "51c5957b-684a-453f-8ab1-8f239999c4d8"

ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["admin"], "permissions": ["members.invite", "members.remove"]},
    },
}

VIEWER_CLAIMS = {
    "sub": "user456",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["viewer"], "permissions": ["projects.read"]},
    },
}

OWNER_CLAIMS = {
    "sub": "owner1",
    "dct": TENANT_ID,
    "tenants": {
        TENANT_ID: {"roles": ["owner"], "permissions": ["members.invite", "members.remove"]},
    },
}

AUTH_HEADER = {"Authorization": "Bearer valid.token"}

# Descope-style user dicts (as returned by the management API)
DESCOPE_USER = {
    "userId": "U2abc123",
    "email": "alice@example.com",
    "name": "Alice Smith",
    "status": "enabled",
    "userTenants": [
        {"tenantId": TENANT_ID, "roleNames": ["admin"]},
    ],
}


def _make_http_status_error(status_code: int = 500, body: str = "error") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/user")
    response = httpx.Response(status_code, request=request, text=body)
    return httpx.HTTPStatusError(f"{status_code} Server Error", request=request, response=response)


# --- Auth enforcement ---


@pytest.mark.anyio
async def test_list_members_rejects_unauthenticated(client):
    response = await client.get("/api/members")
    assert response.status_code == 401


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_rejected_for_viewer(mock_validate, client):
    mock_validate.return_value = VIEWER_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com"},
    )
    assert response.status_code == 403


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_without_tenant_rejected(mock_validate, client):
    mock_validate.return_value = {"sub": "user789", "tenants": {}}
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com"},
    )
    assert response.status_code == 403


# --- Happy path ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.search_tenant_users.return_value = [
        DESCOPE_USER,
        {**DESCOPE_USER, "userId": "U2def456", "email": "bob@example.com", "name": "Bob"},
    ]

    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "members" in data
    assert len(data["members"]) == 2
    mock_descope.search_tenant_users.assert_awaited_once_with(TENANT_ID)


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.invite_user.return_value = DESCOPE_USER

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["member"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "invited"
    assert data["email"] == "new@test.com"
    assert "user" in data
    mock_descope.invite_user.assert_awaited_once_with(
        email="new@test.com",
        tenant_id=TENANT_ID,
        role_names=["member"],
    )


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = "U2abc123"
    mock_descope.load_user.return_value = DESCOPE_USER
    mock_descope.update_user_status.return_value = None

    response = await client.post(
        f"/api/members/{user_id}/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "deactivated"
    assert data["user_id"] == user_id
    mock_descope.update_user_status.assert_awaited_once_with(user_id, "disabled")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member_forwards_the_id_this_api_hands_out(mock_validate, mock_descope, client):
    """The member routes forward their path identifier to Descope unchanged.

    `canonical_user()` sets `id` to the Descope `userId`, so that is what
    POST /api/members/invite and GET /api/members hand callers, and what the UI
    sends back. Descope's user endpoints take `loginIDOrUserID` in the `loginId`
    field, so a userId is a valid value there — probed live, `update/status` and
    `update/tenant/remove` both return 200 for a userId.

    Pinned because an earlier fix here resolved the path parameter through the
    canonical database first, on the theory that this API hands out a Postgres
    UUID. It does not, so the lookup never fired for any real caller while
    adding an untenanted read to three admin routes.
    """
    mock_validate.return_value = ADMIN_CLAIMS
    descope_user_id = "U3J36liQh0rQ39NT2SdRqdEqb4Zs"
    mock_descope.load_user.return_value = DESCOPE_USER
    mock_descope.update_user_status.return_value = None

    response = await client.post(f"/api/members/{descope_user_id}/deactivate", headers=AUTH_HEADER)

    assert response.status_code == 200
    assert response.json()["user_id"] == descope_user_id
    mock_descope.update_user_status.assert_awaited_once_with(descope_user_id, "disabled")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member_forwards_the_id_this_api_hands_out(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    descope_user_id = "U3J36liQh0rQ39NT2SdRqdEqb4Zs"
    mock_descope.load_user.return_value = {**DESCOPE_USER, "userId": descope_user_id}
    mock_descope.remove_user_from_tenant.return_value = None

    response = await client.delete(f"/api/members/{descope_user_id}", headers=AUTH_HEADER)

    assert response.status_code == 200
    assert mock_descope.remove_user_from_tenant.await_args[0][0] == descope_user_id


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = "U2abc123"
    mock_descope.load_user.return_value = DESCOPE_USER
    mock_descope.update_user_status.return_value = None

    response = await client.post(
        f"/api/members/{user_id}/activate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "activated"
    assert data["user_id"] == user_id
    mock_descope.update_user_status.assert_awaited_once_with(user_id, "enabled")


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    user_id = "U2abc123"
    mock_descope.load_user.return_value = DESCOPE_USER
    mock_descope.remove_user_from_tenant.return_value = None

    response = await client.delete(
        f"/api/members/{user_id}",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "removed"
    assert data["user_id"] == user_id
    mock_descope.remove_user_from_tenant.assert_awaited_once_with(user_id, TENANT_ID)


# --- Owner role escalation guard ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_admin_cannot_assign_owner_role(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["owner"]},
    )
    assert response.status_code == 403
    assert "Only owners can assign the owner role" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_owner_can_assign_owner_role(mock_validate, mock_descope, client):
    mock_validate.return_value = OWNER_CLAIMS
    mock_descope.invite_user.return_value = DESCOPE_USER

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com", "role_names": ["owner"]},
    )
    assert response.status_code == 200


# --- Descope API error handling ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_list_members_descope_error(mock_validate, mock_descope, client):
    """Descope API error -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.search_tenant_users.side_effect = _make_http_status_error(500)

    response = await client.get("/api/members", headers=AUTH_HEADER)
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_descope_400(mock_validate, mock_descope, client):
    """Descope 400 (e.g. duplicate) -> 400 with error description."""
    mock_validate.return_value = ADMIN_CLAIMS
    request = httpx.Request("POST", "https://api.descope.com/v1/mgmt/user/create")
    response_obj = httpx.Response(
        400,
        request=request,
        json={"errorDescription": "User already exists"},
    )
    mock_descope.invite_user.side_effect = httpx.HTTPStatusError(
        "400 Bad Request", request=request, response=response_obj
    )

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "dup@test.com"},
    )
    assert response.status_code == 400
    assert "User already exists" in response.json()["detail"]


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_descope_502(mock_validate, mock_descope, client):
    """Descope 500 -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.invite_user.side_effect = _make_http_status_error(500)

    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "new@test.com"},
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_deactivate_member_descope_error(mock_validate, mock_descope, client):
    """Descope API error deactivating -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.return_value = DESCOPE_USER
    mock_descope.update_user_status.side_effect = _make_http_status_error(500)

    response = await client.post(
        "/api/members/U2abc123/deactivate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_activate_member_descope_error(mock_validate, mock_descope, client):
    """Descope API error activating -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.return_value = DESCOPE_USER
    mock_descope.update_user_status.side_effect = _make_http_status_error(500)

    response = await client.post(
        "/api/members/U2abc123/activate",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 502


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member_descope_error(mock_validate, mock_descope, client):
    """Descope API error removing -> 502."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.return_value = DESCOPE_USER
    mock_descope.remove_user_from_tenant.side_effect = _make_http_status_error(500)

    response = await client.delete(
        "/api/members/U2abc123",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 502


# --- Tenant scope of activate/deactivate (#581) ---

OTHER_TENANT_ID = "9d1f6f0e-2a55-4b53-9a0e-6a3f1c1f7a11"
STATUS_ROUTES = ["deactivate", "activate"]


async def _unknown_user_response(mock_descope, client, action):
    """What the route answers for a user Descope does not have."""
    mock_descope.load_user.return_value = {}
    mock_descope.update_user_status.side_effect = _make_http_status_error(404)
    response = await client.post(f"/api/members/Unosuchuser/{action}", headers=AUTH_HEADER)
    mock_descope.update_user_status.reset_mock(side_effect=True)
    return response


@pytest.mark.anyio
@pytest.mark.parametrize("action", STATUS_ROUTES)
@pytest.mark.parametrize(
    "user_tenants",
    [
        [{"tenantId": OTHER_TENANT_ID, "roleNames": ["member"]}],
        [],
        None,
    ],
)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_member_status_refuses_a_user_outside_the_callers_tenant(
    mock_validate, mock_descope, client, action, user_tenants
):
    """The refusal reads exactly like an unknown user, and Descope's status endpoint is never called."""
    mock_validate.return_value = ADMIN_CLAIMS
    unknown = await _unknown_user_response(mock_descope, client, action)
    mock_descope.load_user.return_value = {**DESCOPE_USER, "userTenants": user_tenants}

    response = await client.post(f"/api/members/{DESCOPE_USER['userId']}/{action}", headers=AUTH_HEADER)

    assert response.status_code == unknown.status_code == 502
    assert response.json() == unknown.json()
    assert response.headers["content-type"] == unknown.headers["content-type"]
    mock_descope.load_user.assert_awaited_with(DESCOPE_USER["userId"])
    mock_descope.update_user_status.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("action", STATUS_ROUTES)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_member_status_does_not_call_descope_for_an_unknown_user(mock_validate, mock_descope, client, action):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.return_value = {}

    response = await client.post(f"/api/members/Unosuchuser/{action}", headers=AUTH_HEADER)

    assert response.status_code == 502
    mock_descope.update_user_status.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("action", STATUS_ROUTES)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_member_status_fails_closed_when_the_lookup_fails(mock_validate, mock_descope, client, action):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.side_effect = _make_http_status_error(500)

    response = await client.post(f"/api/members/{DESCOPE_USER['userId']}/{action}", headers=AUTH_HEADER)

    assert response.status_code == 502
    mock_descope.update_user_status.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("action", STATUS_ROUTES)
@pytest.mark.parametrize(
    "other_membership",
    [
        {"tenantId": OTHER_TENANT_ID, "roleNames": ["member"]},
        {"roleNames": ["member"]},
    ],
)
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_member_status_refuses_a_user_shared_with_another_tenant(
    mock_validate, mock_descope, client, action, other_membership
):
    """Status is per account, so a member who also belongs elsewhere is not this tenant's to disable or enable."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.return_value = {
        **DESCOPE_USER,
        "userTenants": [{"tenantId": TENANT_ID, "roleNames": ["admin"]}, other_membership],
    }

    response = await client.post(f"/api/members/{DESCOPE_USER['userId']}/{action}", headers=AUTH_HEADER)

    assert response.status_code == 409
    assert response.json() == {
        "detail": (
            "Status applies to the member's whole account, which also belongs to another tenant. "
            "Remove the member from this tenant instead."
        )
    }
    mock_descope.update_user_status.assert_not_awaited()


# --- Who a tenant admin may act on: the member's roles in the tenant (access.act_on_member) ---

MEMBER_ACTIONS = {
    "deactivate": ("POST", "/deactivate", "update_user_status"),
    "activate": ("POST", "/activate", "update_user_status"),
    "remove": ("DELETE", "", "remove_user_from_tenant"),
}
OWNER_REFUSED = {"detail": "Only owners can deactivate, activate or remove an owner"}
OPERATOR_REFUSED = {"detail": "Only operators can assign or remove the operator role"}
SELF_REFUSED = {"detail": "You cannot deactivate or remove yourself"}


def _member_with(roles: list[str], user_id: str = "U2abc123") -> dict:
    return {**DESCOPE_USER, "userId": user_id, "userTenants": [{"tenantId": TENANT_ID, "roleNames": roles}]}


async def _act(client, route: str, user_id: str):
    method, suffix, _ = MEMBER_ACTIONS[route]
    return await client.request(method, f"/api/members/{user_id}{suffix}", headers=AUTH_HEADER)


@pytest.mark.anyio
@pytest.mark.parametrize("route", sorted(MEMBER_ACTIONS))
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_a_descope_admin_cannot_act_on_the_owner(mock_validate, mock_descope, client, route):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.return_value = _member_with(["owner"])

    response = await _act(client, route, "U2abc123")

    assert (response.status_code, response.json()) == (403, OWNER_REFUSED)
    getattr(mock_descope, MEMBER_ACTIONS[route][2]).assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("route", sorted(MEMBER_ACTIONS))
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_a_descope_owner_acts_on_another_owner(mock_validate, mock_descope, client, route):
    mock_validate.return_value = OWNER_CLAIMS
    mock_descope.load_user.return_value = _member_with(["owner"])

    response = await _act(client, route, "U2abc123")

    assert response.status_code == 200
    getattr(mock_descope, MEMBER_ACTIONS[route][2]).assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("route", sorted(MEMBER_ACTIONS))
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_a_descope_admin_cannot_act_on_an_operator(mock_validate, mock_descope, client, route):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.return_value = _member_with(["operator"])

    response = await _act(client, route, "U2abc123")

    assert (response.status_code, response.json()) == (403, OPERATOR_REFUSED)
    getattr(mock_descope, MEMBER_ACTIONS[route][2]).assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("route", sorted(MEMBER_ACTIONS))
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_a_descope_operator_acts_on_an_operator(mock_validate, mock_descope, client, route):
    mock_validate.return_value = {
        **ADMIN_CLAIMS,
        "tenants": {TENANT_ID: {"roles": ["admin", "operator"], "permissions": []}},
    }
    mock_descope.load_user.return_value = _member_with(["operator"])

    response = await _act(client, route, "U2abc123")

    assert response.status_code == 200
    getattr(mock_descope, MEMBER_ACTIONS[route][2]).assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("route", ["deactivate", "remove"])
@pytest.mark.parametrize("named_by", ["user123", "admin@example.com"], ids=["user id", "login id"])
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_a_descope_admin_neither_deactivates_nor_removes_itself(
    mock_validate, mock_descope, client, route, named_by
):
    """Named by its userId or its login id, the caller is the Descope user its ``sub`` names."""
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.return_value = _member_with(["admin"], user_id=ADMIN_CLAIMS["sub"])

    response = await _act(client, route, named_by)

    assert (response.status_code, response.json()) == (403, SELF_REFUSED)
    mock_descope.load_user.assert_awaited_once_with(named_by)
    getattr(mock_descope, MEMBER_ACTIONS[route][2]).assert_not_awaited()


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_a_failed_descope_member_lookup_removes_nothing(mock_validate, mock_descope, client):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_descope.load_user.side_effect = _make_http_status_error(500)

    response = await _act(client, "remove", "U2abc123")

    assert (response.status_code, response.json()) == (502, {"detail": "Failed to remove member"})
    mock_descope.remove_user_from_tenant.assert_not_awaited()


# --- Input validation ---


@pytest.mark.anyio
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_invalid_email_rejected(mock_validate, client):
    mock_validate.return_value = ADMIN_CLAIMS
    response = await client.post(
        "/api/members/invite",
        headers=AUTH_HEADER,
        json={"email": "not-an-email", "role_names": ["member"]},
    )
    assert response.status_code == 422
