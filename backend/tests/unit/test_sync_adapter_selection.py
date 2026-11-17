"""Which sync adapter the services get, how startup opens it, and what leaving a tenant sends it."""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from expression import Ok
from fastapi import FastAPI

from app import main
from app.dependencies import identity
from app.dependencies.identity import (
    SYNC_ADAPTERS,
    get_permission_service,
    get_reconciliation_service,
    get_role_service,
    get_tenant_service,
    get_user_service,
)
from app.errors.identity import Conflict, IdentityErrorRaised
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import LAST_ROLE_IN_TENANT, STATUS_CHANGED, IdentityProviderAdapter
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.adapters.keycloak import KeycloakSyncAdapter
from app.services.keycloak_admin import ADMIN_CLIENT_ID_VARIABLE, ADMIN_CREDENTIAL_VARIABLE, KeycloakAdminApi
from app.services.reconciliation import ReconciliationService
from app.services.role import RoleService
from app.services.user import UserService
from tests.helpers.keycloak_admin_recorded import (
    DIRECTORY_CLIENT_ID,
    DIRECTORY_CLIENT_SECRET,
    ROLE_CHECK,
    RecordedAdminApi,
)
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


class TestReconciliation:
    """Reconciliation compares the canonical model with Descope: it runs only where Descope is synced to."""

    async def test_descope_deployments_reconcile(self):
        service = await get_reconciliation_service(mock_request(), session=AsyncMock())

        assert isinstance(service, ReconciliationService)

    async def test_keycloak_deployments_refuse_to_reconcile_against_descope(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")

        with pytest.raises(IdentityErrorRaised) as refused:
            await get_reconciliation_service(mock_request(), session=AsyncMock())

        assert refused.value.error == Conflict(message=identity.RECONCILIATION_NEEDS_DESCOPE)


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
        api = RecordedAdminApi(["token", *ROLE_CHECK])
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

    @pytest.mark.parametrize(
        ("answer", "message"),
        [
            (
                httpx.ConnectError("connection refused"),
                "Keycloak gave the admin client's credential check no readable answer: "
                "check KEYCLOAK_ISSUER_URL and that Keycloak is up",
            ),
            (
                httpx.Response(404, json={"errorMessage": "Organizations not enabled for this realm."}),
                "Keycloak answered the admin client's credential check with status 404: "
                "check KEYCLOAK_ISSUER_URL names a realm with organizations enabled",
            ),
        ],
        ids=["unreachable", "realm-wide-404"],
    )
    async def test_a_failed_check_that_is_not_a_refusal_says_so(self, monkeypatch, answer, message):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        set_settings(monkeypatch)
        script = [answer] if isinstance(answer, Exception) else ["token", answer]
        monkeypatch.setattr(main, "outbound_client", recorded_client(RecordedAdminApi(script)))

        with pytest.raises(RuntimeError) as refused:
            await main._start_sync_provider(FakeApp())

        assert str(refused.value) == message

    async def test_a_credential_without_the_write_roles_stops_startup_and_closes_the_client(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        set_settings(
            monkeypatch,
            **{ADMIN_CLIENT_ID_VARIABLE: DIRECTORY_CLIENT_ID, ADMIN_CREDENTIAL_VARIABLE: DIRECTORY_CLIENT_SECRET},
        )
        api = RecordedAdminApi(
            [
                "directory_token",
                "directory_credential_check",
                "directory_service_account_find",
                "directory_service_account_role_mappings",
            ]
        )
        built = []

        def build(*, timeout: float) -> httpx.AsyncClient:
            built.append(recorded_client(api)(timeout=timeout))
            return built[0]

        monkeypatch.setattr(main, "outbound_client", build)

        with pytest.raises(RuntimeError) as refused:
            await main._start_sync_provider(FakeApp())

        assert str(refused.value) == (
            "The Keycloak admin client's service account lacks realm-management roles "
            "['manage-organizations', 'manage-users']: grant it ['manage-organizations', 'manage-users'] "
            "directly (roles reached through a group or a composite realm role are not read) "
            "or name another client in KEYCLOAK_ADMIN_CLIENT_ID"
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


class TestStatusChanges:
    """Only activate and deactivate say the status changed, so a profile update leaves an IdP-side disable."""

    @staticmethod
    def users(status: UserStatus) -> tuple[UserService, AsyncMock, User, uuid.UUID]:
        user = User(id=uuid.uuid4(), email="a@b.test", user_name="a", status=status)
        repo = AsyncMock(spec=UserRepository)
        repo.get.return_value = user
        repo.get_by_email.return_value = None
        repo.exists_in_tenant.return_value = True
        repo.update.side_effect = lambda changed: changed
        adapter = AsyncMock(spec=IdentityProviderAdapter)
        adapter.sync_user.return_value = Ok(None)
        service = UserService(repository=repo, adapter=adapter, publisher=AsyncMock())
        return service, adapter, user, uuid.uuid4()

    async def test_a_profile_update_does_not_say_the_status_changed(self):
        service, adapter, user, tenant_id = self.users(UserStatus.active)

        await service.update_user(tenant_id=tenant_id, user_id=user.id, given_name="Ada")

        adapter.sync_user.assert_awaited_once_with(user_id=user.id, data={"email": "a@b.test", "status": "active"})

    async def test_deactivating_says_the_status_changed(self):
        service, adapter, user, tenant_id = self.users(UserStatus.active)

        await service.deactivate_user(tenant_id=tenant_id, user_id=user.id)

        adapter.sync_user.assert_awaited_once_with(
            user_id=user.id, data={"email": "a@b.test", "status": "inactive", STATUS_CHANGED: True}
        )

    async def test_activating_says_the_status_changed(self):
        service, adapter, user, tenant_id = self.users(UserStatus.inactive)

        await service.activate_user(tenant_id=tenant_id, user_id=user.id)

        adapter.sync_user.assert_awaited_once_with(
            user_id=user.id, data={"email": "a@b.test", "status": "active", STATUS_CHANGED: True}
        )


class TestLosingTheLastRole:
    """RoleService tells the adapter when an unassignment leaves the user no role in the tenant."""

    @staticmethod
    def roles(remaining: list[UserTenantRole]) -> tuple[RoleService, AsyncMock, AsyncMock, Role]:
        role = Role(id=uuid.uuid4(), name="editor", description="", tenant_id=None)
        repo = AsyncMock(spec=RoleRepository)
        repo.get.return_value = role
        assignments = AsyncMock(spec=UserTenantRoleRepository)
        assignments.delete.return_value = True
        assignments.list_by_user_tenant.return_value = remaining
        adapter = AsyncMock(spec=IdentityProviderAdapter)
        adapter.delete_role_assignment.return_value = Ok(None)
        service = RoleService(
            repository=repo,
            permission_repository=AsyncMock(spec=PermissionRepository),
            assignment_repository=assignments,
            adapter=adapter,
            publisher=AsyncMock(),
        )
        return service, assignments, adapter, role

    async def test_unassigning_the_only_role_says_it_was_the_last(self):
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        service, assignments, adapter, role = self.roles([])

        await service.unassign_role_from_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        assignments.list_by_user_tenant.assert_awaited_once_with(user_id, tenant_id)
        adapter.delete_role_assignment.assert_awaited_once_with(
            user_id=user_id,
            tenant_id=tenant_id,
            role_id=role.id,
            data={"role_name": "editor", LAST_ROLE_IN_TENANT: True},
        )

    async def test_the_remaining_roles_are_counted_after_the_commit(self):
        """Counted before the commit, two concurrent unassigns of the last two roles each see the other's row."""
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        service, assignments, adapter, role = self.roles([])
        calls: list[str] = []
        assignments.commit.side_effect = lambda: calls.append("commit")

        async def remaining(*_args) -> list[UserTenantRole]:
            calls.append("count")
            return []

        assignments.list_by_user_tenant.side_effect = remaining

        await service.unassign_role_from_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        assert calls == ["commit", "count"]
        adapter.delete_role_assignment.assert_awaited_once_with(
            user_id=user_id,
            tenant_id=tenant_id,
            role_id=role.id,
            data={"role_name": "editor", LAST_ROLE_IN_TENANT: True},
        )

    async def test_a_failed_count_still_invalidates_the_revoked_role(self):
        """The unassignment is committed: its cached permissions go before the count can fail."""
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        role = Role(id=uuid.uuid4(), name="editor", description="", tenant_id=None)
        repo = AsyncMock(spec=RoleRepository)
        repo.get.return_value = role
        assignments = AsyncMock(spec=UserTenantRoleRepository)
        assignments.delete.return_value = True
        assignments.list_by_user_tenant.side_effect = ConnectionError("database went away")
        publisher = AsyncMock()
        adapter = AsyncMock(spec=IdentityProviderAdapter)
        service = RoleService(
            repository=repo,
            permission_repository=AsyncMock(spec=PermissionRepository),
            assignment_repository=assignments,
            adapter=adapter,
            publisher=publisher,
        )

        with pytest.raises(ConnectionError):
            await service.unassign_role_from_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        assignments.commit.assert_awaited_once_with()
        publisher.publish.assert_awaited_once()
        assert publisher.publish.await_args.kwargs["entity_id"] == role.id
        assert publisher.publish.await_args.kwargs["tenant_id"] == tenant_id
        adapter.delete_role_assignment.assert_not_awaited()

    async def test_unassigning_one_of_two_roles_sends_what_it_sent_before(self):
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        other = UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())
        service, _assignments, adapter, role = self.roles([other])

        await service.unassign_role_from_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        adapter.delete_role_assignment.assert_awaited_once_with(
            user_id=user_id, tenant_id=tenant_id, role_id=role.id, data={"role_name": "editor"}
        )


class FakeConnection:
    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, statement) -> None:
        return None


class FakeEngine:
    """Answers the lifespan's connectivity check; records its disposal."""

    def __init__(self) -> None:
        self.disposed = False

    def connect(self) -> "FakeConnection":
        return FakeConnection()

    async def dispose(self) -> None:
        self.disposed = True


class TestLifespan:
    """The real lifespan runs the Keycloak credential check before the application serves."""

    @pytest.fixture
    def started(self, monkeypatch) -> list[httpx.AsyncClient]:
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        monkeypatch.setenv("DESCOPE_PROJECT_ID", "P-local-test")
        monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "local-test-key")
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.setattr(main, "get_engine", FakeEngine)
        monkeypatch.setattr(main, "init_telemetry", lambda **_kwargs: None)
        built: list[httpx.AsyncClient] = []
        return built

    def build_into(self, monkeypatch, built: list, api: RecordedAdminApi) -> None:
        def build(*, timeout: float) -> httpx.AsyncClient:
            built.append(recorded_client(api)(timeout=timeout))
            return built[-1]

        monkeypatch.setattr(main, "outbound_client", build)

    async def test_a_missing_admin_secret_stops_startup_before_serving(self, monkeypatch, started):
        set_settings(monkeypatch, **{ADMIN_CREDENTIAL_VARIABLE: None})
        api = RecordedAdminApi([])
        self.build_into(monkeypatch, started, api)
        served = []

        with pytest.raises(RuntimeError) as refused:
            async with main.lifespan(FastAPI()):
                served.append(True)

        assert str(refused.value) == "SYNC_PROVIDER is keycloak but KEYCLOAK_ADMIN_CLIENT_SECRET is not set"
        assert served == []
        assert api.sent == []
        assert [client.is_closed for client in started] == [True, True]

    async def test_a_wrong_admin_secret_stops_startup_before_serving(self, monkeypatch, started):
        set_settings(monkeypatch, **{ADMIN_CREDENTIAL_VARIABLE: "wrong"})
        api = RecordedAdminApi(["token_wrong_secret"])
        self.build_into(monkeypatch, started, api)
        served = []

        with pytest.raises(RuntimeError, match="status 401") as refused:
            async with main.lifespan(FastAPI()):
                served.append(True)

        assert served == []
        assert ADMIN_CLIENT_ID_VARIABLE in str(refused.value)
        assert [client.is_closed for client in started] == [True, True]

    async def test_a_failed_descope_client_close_still_closes_the_sync_client_and_the_engine(
        self, monkeypatch, started
    ):
        """Shutdown is a finally chain: a BaseException from one close skips none of the later ones."""
        set_settings(monkeypatch)
        engine = FakeEngine()
        monkeypatch.setattr(main, "get_engine", lambda: engine)
        api = RecordedAdminApi(["token", *ROLE_CHECK])
        self.build_into(monkeypatch, started, api)

        class Interrupted(BaseException):
            pass

        with pytest.raises(Interrupted):
            async with main.lifespan(FastAPI()):
                descope_http, sync_http = started

                async def interrupted_close() -> None:
                    raise Interrupted

                monkeypatch.setattr(descope_http, "aclose", interrupted_close)

        assert sync_http.is_closed
        assert engine.disposed

    async def test_a_working_admin_secret_serves_and_closes_on_shutdown(self, monkeypatch, started):
        set_settings(monkeypatch)
        api = RecordedAdminApi(["token", *ROLE_CHECK])
        self.build_into(monkeypatch, started, api)
        app = FastAPI()

        async with main.lifespan(app):
            assert isinstance(app.state.keycloak_admin, KeycloakAdminApi)

        assert api.script == []
        assert app.state.keycloak_admin is None
        assert [client.is_closed for client in started] == [True, True]
