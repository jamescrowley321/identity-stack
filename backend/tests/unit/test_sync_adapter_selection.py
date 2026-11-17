"""Which sync adapter the services get, how startup opens it, and what leaving a tenant sends it."""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from expression import Ok

from app import main
from app.dependencies import identity
from app.dependencies.identity import (
    SYNC_ADAPTERS,
    get_permission_service,
    get_role_service,
    get_tenant_service,
    get_user_service,
)
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import IdentityProviderAdapter
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.keycloak_admin import ADMIN_CREDENTIAL_VARIABLE
from app.services.user import UserService
from tests.helpers.keycloak_admin_recorded import RecordedAdminApi
from tests.unit.test_keycloak_admin_api import set_settings

pytestmark = pytest.mark.anyio

FACTORIES = [get_user_service, get_role_service, get_permission_service, get_tenant_service]


def mock_request(**state) -> AsyncMock:
    request = AsyncMock()
    request.app.state.descope_client = AsyncMock()
    request.app.state.cache_publisher = AsyncMock()
    for name, value in state.items():
        setattr(request.app.state, name, value)
    return request


class TestSelection:
    def test_the_shipped_providers(self):
        assert sorted(SYNC_ADAPTERS) == ["descope", "keycloak"]

    def test_descope_is_the_default(self):
        assert identity.SYNC_PROVIDER == "descope"

    @pytest.mark.parametrize("factory", FACTORIES)
    async def test_descope_deployments_get_the_descope_adapter(self, monkeypatch, factory):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "descope")
        request = mock_request()

        service = await factory(request=request, session=AsyncMock())

        assert type(service._adapter) is DescopeSyncAdapter
        assert service._adapter._client is request.app.state.descope_client

    @pytest.mark.parametrize("factory", FACTORIES)
    async def test_keycloak_deployments_get_the_keycloak_adapter(self, monkeypatch, factory):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        keycloak_admin = object()
        request = mock_request(keycloak_admin=keycloak_admin)

        service = await factory(request=request, session=AsyncMock())

        assert type(service._adapter) is KeycloakSyncAdapter
        assert service._adapter._api is keycloak_admin


def recorded_client(api: RecordedAdminApi):
    def build(*, timeout: float) -> httpx.AsyncClient:
        api.timeout = timeout
        return httpx.AsyncClient(transport=httpx.MockTransport(api.handle), timeout=timeout)

    return build


class FakeState:
    pass


class FakeApp:
    def __init__(self) -> None:
        self.state = FakeState()


class TestStartup:
    async def test_descope_opens_nothing(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "descope")
        app = FakeApp()

        assert await main._start_sync_provider(app) is None
        assert app.state.keycloak_admin is None

    async def test_an_unknown_provider_stops_startup(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "zitadel-typo")

        with pytest.raises(RuntimeError) as refused:
            await main._start_sync_provider(FakeApp())

        assert str(refused.value) == "SYNC_PROVIDER 'zitadel-typo' is not one of ['descope', 'keycloak']"

    async def test_keycloak_checks_the_credential_and_keeps_the_client(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        set_settings(monkeypatch)
        api = RecordedAdminApi(["token", "credential_check"])
        monkeypatch.setattr(main, "outbound_client", recorded_client(api))
        app = FakeApp()

        client = await main._start_sync_provider(app)

        assert api.script == []
        assert app.state.keycloak_admin._http is client
        assert api.timeout == main.KEYCLOAK_ADMIN_TIMEOUT_SECONDS
        await client.aclose()

    async def test_a_wrong_credential_stops_startup_and_closes_the_client(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        set_settings(monkeypatch, **{ADMIN_CREDENTIAL_VARIABLE: "wrong"})
        api = RecordedAdminApi(["token_wrong_secret"])
        built = []

        def build(*, timeout: float) -> httpx.AsyncClient:
            built.append(recorded_client(api)(timeout=timeout))
            return built[0]

        monkeypatch.setattr(main, "outbound_client", build)

        with pytest.raises(RuntimeError) as refused:
            await main._start_sync_provider(FakeApp())

        assert str(refused.value) == (
            "Keycloak refused the admin client's credential (status 401): "
            "check KEYCLOAK_ADMIN_CLIENT_ID and KEYCLOAK_ADMIN_CLIENT_SECRET"
        )
        assert built[0].is_closed

    async def test_a_missing_credential_stops_startup(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        set_settings(monkeypatch, **{ADMIN_CREDENTIAL_VARIABLE: None})
        api = RecordedAdminApi([])
        monkeypatch.setattr(main, "outbound_client", recorded_client(api))

        with pytest.raises(RuntimeError) as refused:
            await main._start_sync_provider(FakeApp())

        assert str(refused.value) == "SYNC_PROVIDER is keycloak but KEYCLOAK_ADMIN_CLIENT_SECRET is not set"
        assert api.sent == []


class TestLeavingATenant:
    async def test_remove_user_from_tenant_names_the_tenant_left(self):
        tenant_id = uuid.uuid4()
        user = User(id=uuid.uuid4(), email="a@b.test", user_name="a", status=UserStatus.active)
        repo = AsyncMock(spec=UserRepository)
        repo.get.return_value = user
        repo.exists_in_tenant.return_value = True
        assignments = AsyncMock(spec=UserTenantRoleRepository)
        assignments.delete_by_user_tenant.return_value = 1
        adapter = AsyncMock(spec=IdentityProviderAdapter)
        adapter.sync_user.return_value = Ok(None)
        service = UserService(repository=repo, adapter=adapter, assignment_repository=assignments)

        await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=user.id)

        adapter.sync_user.assert_awaited_once_with(
            user_id=user.id, data={"email": "a@b.test", "status": "active", "left_tenant_id": str(tenant_id)}
        )
