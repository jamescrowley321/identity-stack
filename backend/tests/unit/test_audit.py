"""Unit tests for the audit logging service and route integration."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.audit import (
    AuditEvent,
    AuditEventType,
    _get_client_ip,
    audit_event,
    emit_audit_event,
)


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


ADMIN_CLAIMS = {
    "sub": "user123",
    "dct": "tenant-abc",
    "tenants": {
        "tenant-abc": {"roles": ["admin"], "permissions": ["members.invite"]},
    },
}


# --- AuditEvent model tests ---


class TestAuditEventModel:
    def test_creates_with_defaults(self):
        event = AuditEvent(action=AuditEventType.USER_LOGOUT)
        assert event.action == "user_logout"
        assert event.timestamp  # auto-generated
        assert event.result == "success"
        assert event.actor_id is None
        assert event.target == {}

    def test_model_dump_contains_all_fields(self):
        event = AuditEvent(
            action=AuditEventType.ROLE_ASSIGNED,
            actor_id="user1",
            tenant_id="tenant1",
            target={"user_id": "user2", "role_names": ["admin"]},
            ip_address="10.0.0.1",
        )
        data = event.model_dump()
        assert data["action"] == "role_assigned"
        assert data["actor_id"] == "user1"
        assert data["tenant_id"] == "tenant1"
        assert data["target"] == {"user_id": "user2", "role_names": ["admin"]}
        assert data["ip_address"] == "10.0.0.1"
        assert data["result"] == "success"
        assert "timestamp" in data


# --- emit_audit_event tests ---


class TestEmitAuditEvent:
    def test_logs_to_audit_logger(self, caplog):
        event = AuditEvent(
            action=AuditEventType.ACCESS_KEY_CREATED,
            actor_id="actor1",
            target={"key_name": "my-key"},
        )
        with caplog.at_level(logging.INFO, logger="audit"):
            emit_audit_event(event)
        assert any("audit.access_key_created" in r.message for r in caplog.records)
        audit_record = next(r for r in caplog.records if "audit.access_key_created" in r.message)
        assert audit_record.audit_event["actor_id"] == "actor1"
        assert audit_record.audit_event["target"]["key_name"] == "my-key"


# --- _get_client_ip tests ---


class TestGetClientIp:
    def test_uses_x_forwarded_for(self):
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "203.0.113.1, 10.0.0.1"}
        request.client.host = "10.0.0.1"
        assert _get_client_ip(request) == "203.0.113.1"

    def test_falls_back_to_client_host(self):
        request = MagicMock()
        request.headers = {}
        request.client.host = "192.168.1.1"
        assert _get_client_ip(request) == "192.168.1.1"

    def test_returns_none_when_no_client(self):
        request = MagicMock()
        request.headers = {}
        request.client = None
        assert _get_client_ip(request) is None


# --- audit_event convenience function tests ---


class TestAuditEventConvenience:
    def test_extracts_context_from_request(self, caplog):
        request = MagicMock()
        request.state.claims = {"sub": "actor1", "dct": "tenant1"}
        request.headers = {}
        request.client.host = "127.0.0.1"

        with caplog.at_level(logging.INFO, logger="audit"):
            audit_event(request, AuditEventType.USER_REMOVED, {"user_id": "target1"})

        audit_record = next(r for r in caplog.records if "audit.user_removed" in r.message)
        event = audit_record.audit_event
        assert event["actor_id"] == "actor1"
        assert event["tenant_id"] == "tenant1"
        assert event["ip_address"] == "127.0.0.1"
        assert event["target"]["user_id"] == "target1"
        assert event["result"] == "success"

    def test_handles_missing_claims(self, caplog):
        request = MagicMock()
        request.state.claims = None
        request.headers = {}
        request.client.host = "127.0.0.1"

        with caplog.at_level(logging.INFO, logger="audit"):
            audit_event(request, AuditEventType.TENANT_CREATED, {"tenant_name": "acme"})

        audit_record = next(r for r in caplog.records if "audit.tenant_created" in r.message)
        event = audit_record.audit_event
        assert event["actor_id"] is None
        assert event["tenant_id"] is None

    def test_failure_result(self, caplog):
        request = MagicMock()
        request.state.claims = {"sub": "actor1"}
        request.headers = {}
        request.client.host = "10.0.0.1"

        with caplog.at_level(logging.INFO, logger="audit"):
            audit_event(
                request,
                AuditEventType.ACCESS_KEY_DELETED,
                {"key_id": "k1"},
                result="failure",
                detail="not found",
            )

        audit_record = next(r for r in caplog.records if "audit.access_key_deleted" in r.message)
        event = audit_record.audit_event
        assert event["result"] == "failure"
        assert event["detail"] == "not found"


# --- Route integration tests ---


@pytest.mark.anyio
@patch("app.routers.auth.httpx.AsyncClient")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_logout_emits_audit_event(mock_validate, mock_httpx_cls, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_http = AsyncMock()
    mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
    mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.post("/api/auth/logout", headers={"Authorization": "Bearer tok"})
    assert response.status_code == 200
    assert any("audit.user_logout" in r.message for r in caplog.records)


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_assign_roles_emits_audit_event(mock_validate, mock_factory, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.post(
            "/api/roles/assign",
            headers={"Authorization": "Bearer tok"},
            json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["editor"]},
        )
    assert response.status_code == 200
    assert any("audit.role_assigned" in r.message for r in caplog.records)


@pytest.mark.anyio
@patch("app.routers.roles.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_roles_emits_audit_event(mock_validate, mock_factory, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.post(
            "/api/roles/remove",
            headers={"Authorization": "Bearer tok"},
            json={"user_id": "target-user", "tenant_id": "tenant-abc", "role_names": ["editor"]},
        )
    assert response.status_code == 200
    assert any("audit.role_removed" in r.message for r in caplog.records)


@pytest.mark.anyio
@patch("app.routers.accesskeys.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_access_key_emits_audit_event(mock_validate, mock_factory, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_access_key.return_value = {"key": {"id": "k1"}, "cleartext": "secret"}
    mock_factory.return_value = mock_client

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.post(
            "/api/keys",
            headers={"Authorization": "Bearer tok"},
            json={"name": "test-key"},
        )
    assert response.status_code == 200
    assert any("audit.access_key_created" in r.message for r in caplog.records)


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_invite_member_emits_audit_event(mock_validate, mock_factory, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.invite_user.return_value = {"userId": "new-user", "email": "new@test.com"}
    mock_factory.return_value = mock_client

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.post(
            "/api/members/invite",
            headers={"Authorization": "Bearer tok"},
            json={"email": "new@test.com", "role_names": ["member"]},
        )
    assert response.status_code == 200
    assert any("audit.user_invited" in r.message for r in caplog.records)


@pytest.mark.anyio
@patch("app.routers.users.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_remove_member_emits_audit_event(mock_validate, mock_factory, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.delete(
            "/api/members/user1",
            headers={"Authorization": "Bearer tok"},
        )
    assert response.status_code == 200
    assert any("audit.user_removed" in r.message for r in caplog.records)


@pytest.mark.anyio
@patch("app.routers.tenants.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_create_tenant_emits_audit_event(mock_validate, mock_factory, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_client.create_tenant.return_value = {"id": "new-tenant"}
    mock_factory.return_value = mock_client

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.post(
            "/api/tenants",
            headers={"Authorization": "Bearer tok"},
            json={"name": "Acme Corp"},
        )
    assert response.status_code == 200
    assert any("audit.tenant_created" in r.message for r in caplog.records)


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_profile_emits_audit_event(mock_validate, mock_factory, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.patch(
            "/api/profile",
            headers={"Authorization": "Bearer tok"},
            json={"key": "department", "value": "Engineering"},
        )
    assert response.status_code == 200
    assert any("audit.profile_updated" in r.message for r in caplog.records)


@pytest.mark.anyio
@patch("app.routers.attributes.get_descope_client")
@patch("app.middleware.auth.validate_token", new_callable=AsyncMock)
async def test_update_tenant_settings_emits_audit_event(mock_validate, mock_factory, client, caplog):
    mock_validate.return_value = ADMIN_CLAIMS
    mock_client = AsyncMock()
    mock_factory.return_value = mock_client

    with caplog.at_level(logging.INFO, logger="audit"):
        response = await client.patch(
            "/api/tenants/current/settings",
            headers={"Authorization": "Bearer tok"},
            json={"custom_attributes": {"plan": "enterprise"}},
        )
    assert response.status_code == 200
    assert any("audit.tenant_settings_updated" in r.message for r in caplog.records)
