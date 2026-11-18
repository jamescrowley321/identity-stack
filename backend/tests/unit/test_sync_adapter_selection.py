"""Which sync adapter the services get, how startup opens it, and what leaving a tenant sends it."""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from expression import Error, Ok
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
from app.dependencies.keycloak_sync import RepositoryKeycloakLinks
from app.errors.identity import Conflict, Forbidden, IdentityErrorRaised
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import (
    CHANGED_IN_TENANT,
    LAST_ROLE_IN_TENANT,
    PROVIDER_SYNC,
    PROVIDER_SYNC_FAILED,
    STATUS_CHANGED,
    UNANSWERED,
    IdentityProviderAdapter,
    SyncError,
)
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.adapters.keycloak import KeycloakSyncAdapter, LocalDatabaseError
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.adapters.ory import OrySyncAdapter
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    KeycloakAdminApi,
)
from app.services.reconciliation import ReconciliationService
from app.services.role import NOT_A_MEMBER_OF_THE_TENANT, RoleService
from app.services.user import UserService
from tests.helpers.keycloak_admin_recorded import (
    RECORDED,
    ROLE_CHECK,
    TENANT_ID,
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
        assert sorted(SYNC_ADAPTERS) == ["descope", "keycloak", "zitadel"]

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

    async def test_the_keycloak_adapter_reads_its_links_in_the_requests_session(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        session = AsyncMock()

        service = await get_role_service(request=mock_request(keycloak_admin=object()), session=session)

        assert type(service._adapter._links) is RepositoryKeycloakLinks
        assert service._adapter._links._session is session


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

        assert str(refused.value) == "SYNC_PROVIDER 'zitadel-typo' is not one of ['descope', 'keycloak', 'zitadel']"

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
        set_settings(monkeypatch)
        held = [role for role in RECORDED["service_account_roles"]["body"] if role["name"] == "manage-users"]
        api = RecordedAdminApi(["token", *ROLE_CHECK[:3], httpx.Response(200, json=held)])
        built = []

        def build(*, timeout: float) -> httpx.AsyncClient:
            built.append(recorded_client(api)(timeout=timeout))
            return built[0]

        monkeypatch.setattr(main, "outbound_client", build)

        with pytest.raises(RuntimeError) as refused:
            await main._start_sync_provider(FakeApp())

        assert str(refused.value) == (
            "The Keycloak admin client's service account lacks realm-management roles "
            "['manage-organizations', 'query-clients', 'view-events']: grant it "
            "['manage-organizations', 'manage-users', 'query-clients', 'view-events'] "
            "directly (roles reached through a group or a composite realm role are not read) "
            "or name another client in KEYCLOAK_ADMIN_CLIENT_ID"
        )
        assert built[0].is_closed

    async def test_a_credential_that_cannot_manage_the_api_client_stops_startup(self, monkeypatch):
        monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
        set_settings(monkeypatch)
        api = RecordedAdminApi(["token", *ROLE_CHECK[:-1], "api_client_find_none"])
        monkeypatch.setattr(main, "outbound_client", recorded_client(api))

        with pytest.raises(RuntimeError) as refused:
            await main._start_sync_provider(FakeApp())

        assert str(refused.value) == (
            "The Keycloak admin client's service account cannot find the API client named by KEYCLOAK_AUDIENCE: "
            "grant it query-clients and the fine-grained permissions view, manage and map-roles-composite on "
            "that client (keycloak/fixtures/README.md)"
        )

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


class TestLeavingATenantWithAMembership:
    """Where the adapter keeps memberships, a role given in the tenant while the user left it is added back."""

    @staticmethod
    def users(ends_memberships: bool) -> tuple[UserService, AsyncMock, AsyncMock, User, uuid.UUID]:
        service, adapter, user, tenant_id = TestStatusChanges.users(UserStatus.active)
        assignments = AsyncMock(spec=UserTenantRoleRepository)
        assignments.delete_by_user_tenant.return_value = 1
        assignments.list_by_user_tenants.return_value = []
        service._assignment_repository = assignments
        adapter.ends_memberships = ends_memberships
        adapter.sync_role_assignment.return_value = Ok(None)
        return service, adapter, assignments, user, tenant_id

    async def test_a_role_given_while_leaving_gets_its_membership_back(self):
        service, adapter, assignments, user, tenant_id = self.users(ends_memberships=True)
        given = UserTenantRole(user_id=user.id, tenant_id=tenant_id, role_id=uuid.uuid4())
        assignments.list_by_user_tenants.return_value = [given]

        result = await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=user.id)

        assert result == Ok({"status": "removed", "user_id": str(user.id)})
        assignments.list_by_user_tenants.assert_awaited_once_with([(user.id, tenant_id)])
        adapter.sync_role_assignment.assert_awaited_once_with(
            user_id=user.id, tenant_id=tenant_id, role_id=given.role_id
        )

    async def test_every_role_given_while_leaving_is_pushed_back(self):
        """Ending the membership ended its role groups too: each assignment is pushed, not one per membership."""
        service, adapter, assignments, user, tenant_id = self.users(ends_memberships=True)
        given = [UserTenantRole(user_id=user.id, tenant_id=tenant_id, role_id=uuid.uuid4()) for _ in range(2)]
        assignments.list_by_user_tenants.return_value = given

        await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=user.id)

        assert [call.kwargs for call in adapter.sync_role_assignment.await_args_list] == [
            {"user_id": user.id, "tenant_id": tenant_id, "role_id": row.role_id} for row in given
        ]

    async def test_no_role_since_leaves_the_membership_ended(self):
        service, adapter, assignments, user, tenant_id = self.users(ends_memberships=True)

        await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=user.id)

        assignments.list_by_user_tenants.assert_awaited_once_with([(user.id, tenant_id)])
        adapter.sync_role_assignment.assert_not_awaited()

    async def test_a_failed_removal_is_not_recounted(self):
        service, adapter, assignments, user, tenant_id = self.users(ends_memberships=True)
        adapter.sync_user.return_value = Error(SyncError(message="no account", operation="sync_user"))

        await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=user.id)

        assignments.list_by_user_tenants.assert_not_awaited()

    async def test_an_adapter_that_keeps_no_memberships_is_not_recounted(self):
        """Descope and Ory: the push as before, nothing added back."""
        service, adapter, assignments, user, tenant_id = self.users(ends_memberships=False)

        await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=user.id)

        assignments.list_by_user_tenants.assert_not_awaited()
        adapter.sync_role_assignment.assert_not_awaited()


class TestAssigningARole:
    """Where a role assignment writes a provider membership, it is given only to a member of the tenant (#592)."""

    @staticmethod
    def roles(ends_memberships: bool, held: list[UserTenantRole]) -> tuple[RoleService, AsyncMock, AsyncMock, Role]:
        role = Role(id=uuid.uuid4(), name="editor", description="", tenant_id=None)
        repo = AsyncMock(spec=RoleRepository)
        repo.get.return_value = role
        assignments = AsyncMock(spec=UserTenantRoleRepository)
        assignments.get.return_value = None
        assignments.list_by_user_tenant.return_value = held
        assignments.create.side_effect = lambda row: row
        adapter = AsyncMock(spec=IdentityProviderAdapter)
        adapter.ends_memberships = ends_memberships
        adapter.sync_role_assignment.return_value = Ok(None)
        service = RoleService(
            repository=repo,
            permission_repository=AsyncMock(spec=PermissionRepository),
            assignment_repository=assignments,
            adapter=adapter,
            publisher=AsyncMock(),
        )
        return service, assignments, adapter, role

    async def test_a_user_with_no_role_in_the_tenant_is_refused_and_nothing_is_written(self):
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        service, assignments, adapter, role = self.roles(ends_memberships=True, held=[])

        result = await service.assign_role_to_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        assert result == Error(Forbidden(message=NOT_A_MEMBER_OF_THE_TENANT))
        assignments.list_by_user_tenant.assert_awaited_once_with(user_id, tenant_id)
        assignments.create.assert_not_awaited()
        adapter.sync_role_assignment.assert_not_awaited()

    async def test_a_member_of_the_tenant_is_given_the_role(self):
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        member = UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())
        service, assignments, adapter, role = self.roles(ends_memberships=True, held=[member])

        result = await service.assign_role_to_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        assert result.is_ok()
        adapter.sync_role_assignment.assert_awaited_once_with(
            user_id=user_id, tenant_id=tenant_id, role_id=role.id, data={"role_name": role.name}
        )

    async def test_an_adapter_that_keeps_no_memberships_assigns_as_before(self):
        """Descope and Ory: no membership check (that is #592's)."""
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        service, assignments, adapter, role = self.roles(ends_memberships=False, held=[])

        result = await service.assign_role_to_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        assert result.is_ok()
        assignments.list_by_user_tenant.assert_not_awaited()


class TestRevocationsThatFail:
    """Where the adapter keeps memberships, a deactivation or tenant exit the provider did not apply is told."""

    @staticmethod
    def users(ends_memberships: bool) -> tuple[UserService, AsyncMock, User, uuid.UUID]:
        service, adapter, user, tenant_id = TestStatusChanges.users(UserStatus.active)
        service._assignment_repository = AsyncMock(spec=UserTenantRoleRepository)
        service._assignment_repository.delete_by_user_tenant.return_value = 1
        adapter.ends_memberships = ends_memberships
        adapter.sync_user.return_value = Error(SyncError(message="no account", operation="sync_user"))
        return service, adapter, user, tenant_id

    @pytest.mark.parametrize("operation", ["deactivate_user", "remove_user_from_tenant"])
    async def test_keycloak_failures_are_error_logs_and_told_to_the_caller(self, caplog, operation):
        service, _adapter, user, tenant_id = self.users(ends_memberships=True)

        with caplog.at_level("WARNING", logger="app.services.user"):
            result = await getattr(service, operation)(tenant_id=tenant_id, user_id=user.id)

        assert result.ok[PROVIDER_SYNC] == PROVIDER_SYNC_FAILED
        assert [record.levelname for record in caplog.records if record.name == "app.services.user"] == ["ERROR"]

    @pytest.mark.parametrize("operation", ["deactivate_user", "remove_user_from_tenant"])
    async def test_other_adapters_failures_are_warnings_and_the_answer_is_unchanged(self, caplog, operation):
        service, _adapter, user, tenant_id = self.users(ends_memberships=False)

        with caplog.at_level("WARNING", logger="app.services.user"):
            result = await getattr(service, operation)(tenant_id=tenant_id, user_id=user.id)

        assert PROVIDER_SYNC not in result.ok
        assert [record.levelname for record in caplog.records if record.name == "app.services.user"] == ["WARNING"]

    async def test_a_keycloak_push_that_succeeds_tells_nothing(self):
        service, adapter, user, tenant_id = self.users(ends_memberships=True)
        adapter.sync_user.return_value = Ok(None)

        result = await service.deactivate_user(tenant_id=tenant_id, user_id=user.id)

        assert PROVIDER_SYNC not in result.ok


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
            user_id=user.id,
            data={"email": "a@b.test", "status": "inactive", STATUS_CHANGED: True, CHANGED_IN_TENANT: str(tenant_id)},
        )

    async def test_activating_says_the_status_changed(self):
        service, adapter, user, tenant_id = self.users(UserStatus.inactive)

        await service.activate_user(tenant_id=tenant_id, user_id=user.id)

        adapter.sync_user.assert_awaited_once_with(
            user_id=user.id,
            data={"email": "a@b.test", "status": "active", STATUS_CHANGED: True, CHANGED_IN_TENANT: str(tenant_id)},
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


class TestLosingTheLastRoleWithAMembership:
    """For an adapter that keeps memberships, ending the last one is a revocation: a failure is told."""

    @staticmethod
    def roles() -> tuple[RoleService, AsyncMock, AsyncMock, Role]:
        service, assignments, adapter, role = TestLosingTheLastRole.roles([])
        adapter.ends_memberships = True
        assignments.list_by_user_tenants.return_value = []
        return service, assignments, adapter, role

    async def test_a_failed_removal_is_an_error_log_and_told_to_the_caller(self, caplog):
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        service, _assignments, adapter, role = self.roles()
        adapter.delete_role_assignment.return_value = Error(
            SyncError(message="no answer", operation="delete_role_assignment", context={UNANSWERED: True})
        )

        with caplog.at_level("WARNING", logger="app.services.role"):
            result = await service.unassign_role_from_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        assert result.ok[PROVIDER_SYNC] == PROVIDER_SYNC_FAILED
        logged = [record for record in caplog.records if record.name == "app.services.role"]
        assert [record.levelname for record in logged] == ["ERROR"]

    async def test_an_ended_membership_given_a_role_meanwhile_is_added_back(self):
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        service, assignments, adapter, role = self.roles()
        assigned = UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())
        assignments.list_by_user_tenants.return_value = [assigned]
        adapter.sync_role_assignment.return_value = Ok(None)

        result = await service.unassign_role_from_user(user_id=user_id, tenant_id=tenant_id, role_id=role.id)

        assert PROVIDER_SYNC not in result.ok
        assignments.list_by_user_tenants.assert_awaited_once_with([(user_id, tenant_id)])
        adapter.sync_role_assignment.assert_awaited_once_with(
            user_id=user_id, tenant_id=tenant_id, role_id=assigned.role_id
        )

    async def test_an_ended_membership_with_no_role_since_stays_ended(self):
        service, _assignments, adapter, role = self.roles()

        result = await service.unassign_role_from_user(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=role.id)

        assert result.is_ok()
        adapter.sync_role_assignment.assert_not_awaited()

    async def test_an_adapter_that_keeps_no_memberships_is_not_recounted(self):
        """Descope and Ory: the push and its warning as before, nothing added back."""
        service, assignments, adapter, role = TestLosingTheLastRole.roles([])
        adapter.delete_role_assignment.return_value = Error(SyncError(message="down", operation="x"))

        result = await service.unassign_role_from_user(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_id=role.id)

        assert PROVIDER_SYNC not in result.ok
        assignments.list_by_user_tenants.assert_not_awaited()
        adapter.sync_role_assignment.assert_not_awaited()


class TestDeletingARole:
    """Deleting a role ends the memberships it was the last role of, for an adapter that keeps memberships."""

    @staticmethod
    def roles(
        holders: list[UserTenantRole], remaining: dict[tuple[uuid.UUID, uuid.UUID], list[UserTenantRole]]
    ) -> tuple[RoleService, AsyncMock, AsyncMock, Role, list[str]]:
        role = Role(id=uuid.uuid4(), name="editor", description="", tenant_id=None)
        calls: list[str] = []
        repo = AsyncMock(spec=RoleRepository)
        repo.get.return_value = role
        repo.lock.side_effect = lambda *_args: calls.append("lock")
        repo.delete.side_effect = lambda *_args: calls.append("delete") or True
        repo.commit.side_effect = lambda: calls.append("commit")
        assignments = AsyncMock(spec=UserTenantRoleRepository)

        async def held(*_args) -> list[UserTenantRole]:
            calls.append("holders")
            return holders

        async def left(pairs) -> list[UserTenantRole]:
            calls.append("count")
            return [row for pair in pairs for row in remaining.get(pair, [])]

        assignments.list_by_role.side_effect = held
        assignments.list_by_user_tenants.side_effect = left
        adapter = AsyncMock(spec=IdentityProviderAdapter)
        adapter.ends_memberships = True
        adapter.delete_role.return_value = Ok(None)
        adapter.delete_role_assignment.return_value = Ok(None)
        service = RoleService(
            repository=repo,
            permission_repository=AsyncMock(spec=PermissionRepository),
            assignment_repository=assignments,
            adapter=adapter,
            publisher=AsyncMock(),
        )
        return service, assignments, adapter, role, calls

    async def test_a_user_whose_last_role_it_was_leaves_and_one_with_another_role_stays(self):
        alone, kept, tenant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        role_id = uuid.uuid4()
        holders = [
            UserTenantRole(user_id=alone, tenant_id=tenant_id, role_id=role_id),
            UserTenantRole(user_id=kept, tenant_id=tenant_id, role_id=role_id),
        ]
        remaining = {
            (alone, tenant_id): [],
            (kept, tenant_id): [UserTenantRole(user_id=kept, tenant_id=tenant_id, role_id=uuid.uuid4())],
        }
        service, assignments, adapter, role, _calls = self.roles(holders, remaining)

        result = await service.delete_role(role_id=role.id)

        assert result == Ok({"status": "deleted", "name": "editor"})
        assignments.list_by_role.assert_awaited_once_with(role.id)
        assert [set(call.args[0]) for call in assignments.list_by_user_tenants.await_args_list] == [
            {(alone, tenant_id), (kept, tenant_id)},
            {(alone, tenant_id)},
        ]
        adapter.delete_role.assert_awaited_once_with(role_id=role.id, data={"name": "editor", "tenant_id": None})
        adapter.delete_role_assignment.assert_awaited_once_with(
            user_id=alone,
            tenant_id=tenant_id,
            role_id=role.id,
            data={"role_name": "editor", LAST_ROLE_IN_TENANT: True},
        )

    async def test_the_holders_are_read_under_the_role_lock_before_the_delete_and_counted_after_the_commit(self):
        """The delete cascades to the assignments: read after it, there are none left to name the holders.
        The role's row is locked first, so no assignment of it commits unseen between the read and the delete.
        Counted once after the commit, and once more after the removals (a role assigned meanwhile)."""
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        holders = [UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())]
        service, _assignments, adapter, role, calls = self.roles(holders, {(user_id, tenant_id): []})

        await service.delete_role(role_id=role.id)

        assert calls == ["lock", "holders", "delete", "commit", "count", "count"]
        adapter.delete_role_assignment.assert_awaited_once()

    async def test_the_pushes_stop_at_the_first_the_provider_gave_no_answer_to(self, caplog):
        """Each push would wait out the same timeout: the rest are logged as not pushed."""
        tenant_id, role_id = uuid.uuid4(), uuid.uuid4()
        users = sorted(uuid.uuid4() for _ in range(3))
        holders = [UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=role_id) for user_id in users]
        service, _assignments, adapter, role, _calls = self.roles(holders, {})
        adapter.delete_role_assignment.return_value = Error(
            SyncError(message="no answer", operation="delete_role_assignment", context={UNANSWERED: True})
        )

        with caplog.at_level("ERROR", logger="app.services.role"):
            result = await service.delete_role(role_id=role.id)

        assert result == Ok({"status": "deleted", "name": "editor", PROVIDER_SYNC: PROVIDER_SYNC_FAILED})
        adapter.delete_role_assignment.assert_awaited_once()
        logged = [record.getMessage() for record in caplog.records if record.name == "app.services.role"]
        assert [str(user_id) in message for message, user_id in zip(logged, users, strict=True)] == [True] * 3

    async def test_a_refused_push_does_not_stop_the_others(self):
        tenant_id, role_id = uuid.uuid4(), uuid.uuid4()
        users = sorted(uuid.uuid4() for _ in range(2))
        holders = [UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=role_id) for user_id in users]
        service, _assignments, adapter, role, _calls = self.roles(holders, {})
        adapter.delete_role_assignment.return_value = Error(
            SyncError(message="USER_NOT_IN_KEYCLOAK", operation="delete_role_assignment", context={"status": None})
        )

        await service.delete_role(role_id=role.id)

        assert [call.kwargs["user_id"] for call in adapter.delete_role_assignment.await_args_list] == users

    async def test_a_membership_given_a_role_while_it_was_ending_is_added_back(self):
        """An assign racing the removal pushed its membership first; the removal undid it."""
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        holders = [UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())]
        service, assignments, adapter, role, _calls = self.roles(holders, {})
        assigned = UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())
        assignments.list_by_user_tenants.side_effect = [[], [assigned]]
        adapter.sync_role_assignment.return_value = Ok(None)

        await service.delete_role(role_id=role.id)

        adapter.delete_role_assignment.assert_awaited_once()
        adapter.sync_role_assignment.assert_awaited_once_with(
            user_id=user_id, tenant_id=tenant_id, role_id=assigned.role_id
        )

    async def test_one_membership_per_user_and_tenant_whatever_the_rows(self):
        user_id, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        role_id = uuid.uuid4()
        holders = [
            UserTenantRole(user_id=user_id, tenant_id=first, role_id=role_id),
            UserTenantRole(user_id=user_id, tenant_id=second, role_id=role_id),
        ]
        service, _assignments, adapter, role, _calls = self.roles(
            holders, {(user_id, first): [], (user_id, second): []}
        )

        await service.delete_role(role_id=role.id)

        ended = {call.kwargs["tenant_id"]: call.kwargs for call in adapter.delete_role_assignment.await_args_list}
        assert ended == {
            tenant_id: {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "role_id": role.id,
                "data": {"role_name": "editor", LAST_ROLE_IN_TENANT: True},
            }
            for tenant_id in (first, second)
        }

    async def test_a_failed_membership_push_is_logged_as_an_error_and_told_to_the_caller(self, caplog):
        user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
        holders = [UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=uuid.uuid4())]
        service, _assignments, adapter, role, _calls = self.roles(holders, {(user_id, tenant_id): []})
        adapter.delete_role_assignment.return_value = Error(
            SyncError(message="USER_NOT_IN_KEYCLOAK", operation="delete_role_assignment")
        )

        with caplog.at_level("WARNING", logger="app.services.role"):
            result = await service.delete_role(role_id=role.id)

        assert result == Ok({"status": "deleted", "name": "editor", PROVIDER_SYNC: PROVIDER_SYNC_FAILED})
        logged = [record for record in caplog.records if record.name == "app.services.role"]
        assert [record.levelname for record in logged] == ["ERROR"]

    async def test_a_database_failure_under_the_pushes_leaves_no_membership_unlogged(self, caplog):
        """The real adapter: each failed push is a SyncError, so every holder is tried and logged, nothing raises."""
        tenant_id, role_id = uuid.UUID(TENANT_ID), uuid.uuid4()
        users = sorted(uuid.uuid4() for _ in range(2))
        holders = [UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=role_id) for user_id in users]
        service, assignments, _adapter, role, _calls = self.roles(holders, {})
        # The role's own delete finds Keycloak unreachable; the membership pushes fail on the database first.
        api = RecordedAdminApi([httpx.ConnectError("connection refused")])
        links = AsyncMock()
        links.organization_alias.side_effect = LocalDatabaseError(
            "the tenant link read failed in the service's database: TimeoutError"
        )
        service._adapter = KeycloakSyncAdapter(api.api(), links)

        with caplog.at_level("ERROR", logger="app.services.role"):
            result = await service.delete_role(role_id=role.id)

        assert result == Ok({"status": "deleted", "name": "editor", PROVIDER_SYNC: PROVIDER_SYNC_FAILED})
        logged = [record for record in caplog.records if record.name == "app.services.role"]
        levels = {
            user_id: [record.levelname for record in logged if str(user_id) in record.getMessage()] for user_id in users
        }
        assert levels == {user_id: ["ERROR"] for user_id in users}
        assert api.script == []

    async def test_an_adapter_that_keeps_no_memberships_is_sent_what_it_was_sent_before(self):
        """Descope and Ory: the role delete alone, no holder read and no assignment push."""
        service, assignments, adapter, role, _calls = self.roles([], {})
        adapter.ends_memberships = False

        await service.delete_role(role_id=role.id)

        adapter.delete_role.assert_awaited_once_with(role_id=role.id, data={"name": "editor", "tenant_id": None})
        service._repository.lock.assert_not_awaited()
        assignments.list_by_role.assert_not_awaited()
        adapter.delete_role_assignment.assert_not_awaited()

    def test_only_the_keycloak_adapter_keeps_memberships(self):
        adapters = [
            IdentityProviderAdapter,
            DescopeSyncAdapter,
            OrySyncAdapter,
            NoOpSyncAdapter,
            KeycloakSyncAdapter,
            ZitadelSyncAdapter,
        ]

        assert {adapter.__name__: adapter.ends_memberships for adapter in adapters} == {
            "IdentityProviderAdapter": False,
            "DescopeSyncAdapter": False,
            "OrySyncAdapter": False,
            "NoOpSyncAdapter": False,
            "KeycloakSyncAdapter": True,
            "ZitadelSyncAdapter": False,
        }


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

    async def test_a_refused_start_also_shuts_down_telemetry_and_disposes_the_engine(self, monkeypatch, started):
        """Both were started before the sync provider: a startup it stops leaves neither open."""
        set_settings(monkeypatch, **{ADMIN_CREDENTIAL_VARIABLE: "wrong"})
        engine = FakeEngine()
        monkeypatch.setattr(main, "get_engine", lambda: engine)
        shut_down = []
        monkeypatch.setattr(main, "shutdown_telemetry", lambda: shut_down.append(True))
        self.build_into(monkeypatch, started, RecordedAdminApi(["token_wrong_secret"]))

        with pytest.raises(RuntimeError, match="status 401"):
            async with main.lifespan(FastAPI()):
                pass

        assert (engine.disposed, shut_down) == (True, [True])
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
