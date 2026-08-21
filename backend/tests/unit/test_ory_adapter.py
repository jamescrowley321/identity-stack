"""Unit tests for OrySyncAdapter (outbound canonical→Ory sync)."""

import uuid
from unittest.mock import AsyncMock

import pytest
from expression import Result

from app.services.adapters.base import IdentityProviderAdapter
from app.services.adapters.ory import OrySyncAdapter


def _client() -> AsyncMock:
    client = AsyncMock()
    client.upsert_identity = AsyncMock(return_value=None)
    client.upsert_organization = AsyncMock(return_value="org-123")
    return client


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TestSyncUser:
    @pytest.mark.anyio
    async def test_upserts_identity_with_traits(self):
        client = _client()
        adapter = OrySyncAdapter(client)
        result = await adapter.sync_user(
            user_id=uuid.uuid4(),
            data={"email": "u@example.com", "given_name": "Ada", "family_name": "Lovelace", "status": "active"},
        )
        assert result.is_ok()
        client.upsert_identity.assert_awaited_once_with(
            email="u@example.com",
            traits={"email": "u@example.com", "given_name": "Ada", "family_name": "Lovelace"},
        )

    @pytest.mark.anyio
    async def test_missing_email_returns_error_without_client_call(self):
        client = _client()
        adapter = OrySyncAdapter(client)
        result = await adapter.sync_user(user_id=uuid.uuid4(), data={"given_name": "Ada"})
        assert result.is_error()
        assert result.error.operation == "sync_user"
        client.upsert_identity.assert_not_awaited()

    @pytest.mark.anyio
    async def test_client_failure_returns_syncerror(self):
        client = _client()
        client.upsert_identity = AsyncMock(side_effect=RuntimeError("ory 500"))
        adapter = OrySyncAdapter(client)
        result = await adapter.sync_user(user_id=uuid.uuid4(), data={"email": "u@example.com"})
        assert result.is_error()
        assert result.error.operation == "sync_user"
        assert "ory 500" in result.error.message

    @pytest.mark.anyio
    async def test_omits_empty_optional_traits(self):
        client = _client()
        adapter = OrySyncAdapter(client)
        await adapter.sync_user(user_id=uuid.uuid4(), data={"email": "u@example.com", "given_name": ""})
        client.upsert_identity.assert_awaited_once_with(email="u@example.com", traits={"email": "u@example.com"})


class TestCanonicalOnlyNoOps:
    """Roles and permissions stay canonical — these must never call Ory."""

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "call",
        [
            lambda a: a.sync_role(role_id=uuid.uuid4(), data={"name": "admin"}),
            lambda a: a.sync_permission(permission_id=uuid.uuid4(), data={"name": "read"}),
            lambda a: a.delete_role(role_id=uuid.uuid4()),
            lambda a: a.delete_permission(permission_id=uuid.uuid4()),
            lambda a: a.delete_user(user_id=uuid.uuid4()),
            lambda a: a.delete_tenant(tenant_id=uuid.uuid4()),
            lambda a: a.sync_role_assignment(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=uuid.uuid4()),
            lambda a: a.delete_role_assignment(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=uuid.uuid4()),
        ],
    )
    async def test_noop_returns_ok_and_calls_nothing(self, call):
        client = _client()
        adapter = OrySyncAdapter(client)
        result = await call(adapter)
        assert result.is_ok()
        client.upsert_identity.assert_not_awaited()
        client.upsert_organization.assert_not_awaited()


class TestSyncTenantGating:
    @pytest.mark.anyio
    async def test_disabled_by_default_is_noop(self):
        client = _client()
        adapter = OrySyncAdapter(client)  # enable_organizations defaults False
        result = await adapter.sync_tenant(tenant_id=uuid.uuid4(), data={"name": "Acme"})
        assert result.is_ok()
        client.upsert_organization.assert_not_awaited()

    @pytest.mark.anyio
    async def test_enabled_creates_organization(self):
        client = _client()
        adapter = OrySyncAdapter(client, enable_organizations=True)
        result = await adapter.sync_tenant(tenant_id=uuid.uuid4(), data={"name": "Acme"})
        assert result.is_ok()
        client.upsert_organization.assert_awaited_once_with(label="Acme")

    @pytest.mark.anyio
    async def test_enabled_missing_name_returns_error(self):
        client = _client()
        adapter = OrySyncAdapter(client, enable_organizations=True)
        result = await adapter.sync_tenant(tenant_id=uuid.uuid4(), data={})
        assert result.is_error()
        assert result.error.operation == "sync_tenant"
        client.upsert_organization.assert_not_awaited()

    @pytest.mark.anyio
    async def test_enabled_client_failure_returns_syncerror(self):
        client = _client()
        client.upsert_organization = AsyncMock(side_effect=RuntimeError("org fail"))
        adapter = OrySyncAdapter(client, enable_organizations=True)
        result = await adapter.sync_tenant(tenant_id=uuid.uuid4(), data={"name": "Acme"})
        assert result.is_error()
        assert "org fail" in result.error.message


class TestContract:
    def test_implements_adapter_interface(self):
        adapter = OrySyncAdapter(_client())
        assert isinstance(adapter, IdentityProviderAdapter)

    @pytest.mark.anyio
    async def test_returns_result_type(self):
        adapter = OrySyncAdapter(_client())
        result = await adapter.sync_user(user_id=uuid.uuid4(), data={"email": "u@example.com"})
        assert isinstance(result, Result)
