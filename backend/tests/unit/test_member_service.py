"""Unit tests for the canonical tenant members (#677): MemberService, UserService and RoleService."""

import uuid
from unittest.mock import AsyncMock

import pytest
from expression import Error, Ok

from app.domain import access
from app.errors.identity import Conflict, Forbidden, NotFound, ValidationError
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import (
    CHANGED_IN_TENANT,
    CREATED_IN_TENANT,
    PROVIDER_SYNC,
    PROVIDER_SYNC_FAILED,
    STATUS_CHANGED,
    IdentityProviderAdapter,
    SyncError,
)
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.members import MemberService
from app.services.role import RoleService
from app.services.user import MemberRefusal, MemberStatus, UserService

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _user(**overrides) -> User:
    fields = {
        "id": uuid.uuid4(),
        "email": "alice@example.com",
        "user_name": "alice",
        "given_name": "Alice",
        "family_name": "Smith",
        "status": UserStatus.active,
    }
    return User(**{**fields, **overrides})


def _assignment(user: User, tenant: uuid.UUID, role: Role) -> UserTenantRole:
    return UserTenantRole(user_id=user.id, tenant_id=tenant, role_id=role.id)


class _Deployment:
    """A UserService and a RoleService over mocked repositories and one adapter, and the MemberService over them."""

    def __init__(self, *, ends_memberships: bool) -> None:
        self.users = AsyncMock(spec=UserRepository)
        self.roles = AsyncMock(spec=RoleRepository)
        self.assignments = AsyncMock(spec=UserTenantRoleRepository)
        self.adapter = AsyncMock(spec=IdentityProviderAdapter)
        self.adapter.ends_memberships = ends_memberships
        self.adapter.sync_user.return_value = Ok(None)
        self.adapter.sync_role_assignment.return_value = Ok(None)
        publisher = AsyncMock(spec=CacheInvalidationPublisher)
        self.user_service = UserService(
            repository=self.users, adapter=self.adapter, assignment_repository=self.assignments, publisher=publisher
        )
        self.role_service = RoleService(
            repository=self.roles,
            permission_repository=AsyncMock(spec=PermissionRepository),
            assignment_repository=self.assignments,
            adapter=self.adapter,
            publisher=publisher,
        )
        self.members = MemberService(users=self.user_service, roles=self.role_service)


@pytest.fixture(params=[True, False], ids=["keeps memberships", "keeps none"])
def deployment(request) -> _Deployment:
    return _Deployment(ends_memberships=request.param)


class TestListMembers:
    async def test_each_member_comes_with_its_roles_in_the_tenant(self, deployment):
        alice = _user()
        bob = _user(email="bob@example.com", user_name="bob", given_name="", family_name="", status=UserStatus.inactive)
        deployment.users.list_tenant_members.return_value = [(alice, "admin"), (alice, "member"), (bob, "viewer")]

        listed = await deployment.members.list_members(str(TENANT))

        assert listed == Ok(
            {
                "members": [
                    {
                        "userId": str(alice.id),
                        "email": alice.email,
                        "name": "Alice Smith",
                        "status": MemberStatus.ENABLED,
                        "roleNames": ["admin", "member"],
                    },
                    {
                        "userId": str(bob.id),
                        "email": bob.email,
                        "name": "bob",
                        "status": MemberStatus.DISABLED,
                        "roleNames": ["viewer"],
                    },
                ]
            }
        )
        deployment.users.list_tenant_members.assert_awaited_once_with(TENANT)


def _roles_named(*roles: Role):
    """``RoleRepository.get_by_name`` over global roles."""
    by_name = {role.name: role for role in roles}
    return lambda name, tenant_id=None: by_name.get(name) if tenant_id is None else None


class TestInviteMember:
    async def test_a_new_email_creates_the_user_and_its_membership_in_one_commit_then_pushes_both(self, deployment):
        member_role = Role(id=uuid.uuid4(), name="member")
        created = _user(email="new@example.com", user_name="new@example.com", given_name="", family_name="")
        deployment.roles.get_by_name.side_effect = _roles_named(member_role)
        deployment.roles.get.return_value = member_role
        deployment.users.email_taken.return_value = False
        deployment.users.create.return_value = created
        deployment.assignments.get.return_value = None
        deployment.assignments.create.side_effect = lambda row: row

        invited = await deployment.members.invite_member(
            email="new@example.com", tenant_id=str(TENANT), role_names=["member"]
        )

        assert invited == Ok(
            {
                "status": "invited",
                "email": "new@example.com",
                "user": {"userId": str(created.id), "email": "new@example.com", "roleNames": ["member"]},
            }
        )
        deployment.users.commit.assert_awaited_once()
        deployment.assignments.commit.assert_not_awaited()
        deployment.adapter.sync_user.assert_awaited_once_with(
            user_id=created.id,
            data={"email": "new@example.com", "status": "active", CREATED_IN_TENANT: str(TENANT)},
        )
        deployment.adapter.sync_role_assignment.assert_awaited_once_with(
            user_id=created.id, tenant_id=TENANT, role_id=member_role.id, data={"role_name": "member"}
        )

    async def test_the_email_is_checked_and_stored_in_one_spelling(self, deployment):
        member_role = Role(id=uuid.uuid4(), name="member")
        deployment.roles.get_by_name.side_effect = _roles_named(member_role)
        deployment.roles.get.return_value = member_role
        deployment.users.email_taken.return_value = False
        deployment.users.create.side_effect = lambda row: row
        deployment.assignments.get.return_value = None
        deployment.assignments.create.side_effect = lambda row: row

        invited = await deployment.members.invite_member(
            email=" New@Example.com ", tenant_id=str(TENANT), role_names=["member"]
        )

        deployment.users.email_taken.assert_awaited_once_with("new@example.com")
        stored = deployment.users.create.await_args.args[0]
        assert (stored.email, stored.user_name) == ("new@example.com", "new@example.com")
        assert (invited.ok["email"], invited.ok["user"]["email"]) == ("new@example.com", "new@example.com")

    @pytest.mark.parametrize("failing", ["sync_user", "sync_role_assignment"])
    async def test_a_push_that_failed_is_told(self, deployment, failing):
        """The invitation is committed; the answer says the provider does not hold all of it yet."""
        member_role = Role(id=uuid.uuid4(), name="member")
        created = _user(email="new@example.com")
        deployment.roles.get_by_name.side_effect = _roles_named(member_role)
        deployment.roles.get.return_value = member_role
        deployment.users.email_taken.return_value = False
        deployment.users.create.return_value = created
        deployment.assignments.get.return_value = None
        deployment.assignments.create.side_effect = lambda row: row
        getattr(deployment.adapter, failing).return_value = Error(SyncError(message="Keycloak is down"))

        invited = await deployment.members.invite_member(
            email="new@example.com", tenant_id=str(TENANT), role_names=["member"]
        )

        assert invited == Ok(
            {
                "status": "invited",
                "email": "new@example.com",
                "user": {"userId": str(created.id), "email": "new@example.com", "roleNames": ["member"]},
                PROVIDER_SYNC: PROVIDER_SYNC_FAILED,
            }
        )
        deployment.users.commit.assert_awaited_once()

    async def test_an_existing_account_is_refused_and_given_nothing_whatever_the_adapter(self, deployment):
        """Another tenant's account is neither enrolled nor described (the #693 hole is not repeated here)."""
        member_role = Role(id=uuid.uuid4(), name="member")
        deployment.roles.get_by_name.side_effect = _roles_named(member_role)
        deployment.users.email_taken.return_value = True

        invited = await deployment.members.invite_member(
            email="alice@example.com", tenant_id=str(TENANT), role_names=["member"]
        )

        assert invited == Error(Conflict(message=MemberRefusal.ACCOUNT_EXISTS))
        deployment.users.create.assert_not_awaited()
        deployment.assignments.create.assert_not_awaited()
        deployment.users.commit.assert_not_awaited()
        deployment.adapter.sync_user.assert_not_awaited()
        deployment.adapter.sync_role_assignment.assert_not_awaited()

    async def test_a_role_that_fails_leaves_neither_the_user_nor_any_role(self, deployment):
        """The second role is out of the tenant's scope: the whole invitation rolls back."""
        member_role = Role(id=uuid.uuid4(), name="member")
        other_tenants = Role(id=uuid.uuid4(), name="editor", tenant_id=OTHER_TENANT)
        created = _user(email="new@example.com")
        roles = {member_role.id: member_role, other_tenants.id: other_tenants}
        deployment.roles.get.side_effect = roles.get
        deployment.users.email_taken.return_value = False
        deployment.users.create.return_value = created
        deployment.assignments.get.return_value = None
        deployment.assignments.create.side_effect = lambda row: row

        created_member = await deployment.user_service.create_member(
            tenant_id=TENANT,
            email="new@example.com",
            stage_memberships=lambda user_id: deployment.role_service.stage_member_roles(
                user_id=user_id, tenant_id=TENANT, role_ids=[member_role.id, other_tenants.id]
            ),
        )

        assert created_member.is_error()
        deployment.users.rollback.assert_awaited_once()
        deployment.users.commit.assert_not_awaited()
        deployment.assignments.commit.assert_not_awaited()
        deployment.adapter.sync_user.assert_not_awaited()
        deployment.adapter.sync_role_assignment.assert_not_awaited()

    async def test_an_unknown_role_creates_nobody(self, deployment):
        deployment.roles.get_by_name.return_value = None

        invited = await deployment.members.invite_member(
            email="new@example.com", tenant_id=str(TENANT), role_names=["nope"]
        )

        assert invited == Error(ValidationError(message="Role 'nope' not found"))
        deployment.users.create.assert_not_awaited()
        deployment.adapter.sync_user.assert_not_awaited()

    async def test_the_tenants_own_role_is_preferred_to_a_global_one_of_that_name(self, deployment):
        own = Role(id=uuid.uuid4(), name="editor", tenant_id=TENANT)
        deployment.roles.get_by_name.side_effect = lambda name, tenant_id=None: own if tenant_id == TENANT else None

        assert await deployment.role_service.assignable_roles(tenant_id=TENANT, role_names=["editor"]) == Ok([own.id])


class TestSetMemberStatus:
    @pytest.mark.parametrize(
        ("status", "pushed"), [(MemberStatus.DISABLED, "inactive"), (MemberStatus.ENABLED, "active")]
    )
    async def test_a_member_of_this_tenant_alone_is_set_and_pushed(self, deployment, status, pushed):
        user = _user(status=UserStatus.inactive if status is MemberStatus.ENABLED else UserStatus.active)
        deployment.users.get.return_value = user
        deployment.users.exists_in_tenant.return_value = True
        deployment.users.update.side_effect = lambda row: row
        deployment.assignments.list_by_user.return_value = [_assignment(user, TENANT, Role(name="member"))]

        changed = await deployment.members.set_member_status(str(user.id), str(TENANT), status)

        assert changed.is_ok(), changed
        # The user's row is held from the tenant check to the status commit.
        deployment.users.lock.assert_awaited_once_with(user.id)
        deployment.adapter.sync_user.assert_awaited_once_with(
            user_id=user.id,
            data={"email": user.email, "status": pushed, STATUS_CHANGED: True, CHANGED_IN_TENANT: str(TENANT)},
        )

    @pytest.mark.parametrize("status", [MemberStatus.DISABLED, MemberStatus.ENABLED])
    async def test_a_member_of_another_tenant_too_is_not_this_tenants_to_set(self, deployment, status):
        user = _user()
        deployment.assignments.list_by_user.return_value = [
            _assignment(user, TENANT, Role(name="member")),
            _assignment(user, OTHER_TENANT, Role(name="member")),
        ]

        changed = await deployment.members.set_member_status(str(user.id), str(TENANT), status)

        assert changed == Error(Conflict(message=MemberRefusal.IN_ANOTHER_TENANT))
        deployment.users.update.assert_not_awaited()
        deployment.adapter.sync_user.assert_not_awaited()

    @pytest.mark.parametrize("user_id", [str(uuid.uuid4()), "not-a-uuid"], ids=["outsider or unknown", "malformed"])
    @pytest.mark.parametrize("status", [MemberStatus.DISABLED, MemberStatus.ENABLED])
    async def test_a_user_outside_the_tenant_is_refused_alike(self, deployment, status, user_id):
        deployment.assignments.list_by_user.return_value = []

        changed = await deployment.members.set_member_status(user_id, str(TENANT), status)

        assert changed == Error(Forbidden(message=MemberRefusal.NOT_IN_TENANT))
        deployment.adapter.sync_user.assert_not_awaited()

    async def test_invited_is_not_a_status_a_member_is_set_to(self, deployment):
        changed = await deployment.members.set_member_status(str(uuid.uuid4()), str(TENANT), MemberStatus.INVITED)

        assert changed.is_error() and isinstance(changed.error, ValidationError)


class TestTenantMember:
    """The member as ``access.act_on_member`` reads it: its canonical id and its roles in this tenant only."""

    async def test_a_member_comes_with_its_roles_in_this_tenant(self, deployment):
        user = _user()
        owner = Role(id=uuid.uuid4(), name="owner", description="", tenant_id=None)
        operator = Role(id=uuid.uuid4(), name="operator", description="", tenant_id=None)
        deployment.assignments.list_by_user_tenant.return_value = [
            _assignment(user, TENANT, owner),
            _assignment(user, TENANT, operator),
        ]
        deployment.roles.get.side_effect = {owner.id: owner, operator.id: operator}.get

        found = await deployment.members.tenant_member(str(user.id).upper(), str(TENANT))

        assert found == Ok(access.TenantMember(user_id=str(user.id), roles=frozenset({"owner", "operator"})))
        deployment.assignments.list_by_user_tenant.assert_awaited_once_with(user.id, TENANT)

    @pytest.mark.parametrize("user_id", [str(uuid.uuid4()), "not-a-uuid"], ids=["unknown or outsider", "not an id"])
    async def test_a_user_the_tenant_does_not_have_holds_no_role(self, deployment, user_id):
        deployment.assignments.list_by_user_tenant.return_value = []

        found = await deployment.members.tenant_member(user_id, str(TENANT))

        assert found == Ok(access.TenantMember(user_id=user_id, roles=frozenset()))


class TestRemoveMember:
    async def test_an_unknown_user_reads_as_an_outsider(self, deployment):
        deployment.users.get.return_value = None

        removed = await deployment.members.remove_member(str(uuid.uuid4()), str(TENANT))

        assert removed == Error(Forbidden(message=MemberRefusal.NOT_IN_TENANT))

    async def test_a_member_leaves_the_tenant_and_the_provider_is_told(self, deployment):
        user = _user()
        deployment.users.get.return_value = user
        deployment.users.exists_in_tenant.return_value = True
        deployment.assignments.delete_by_user_tenant.return_value = 1
        deployment.assignments.list_by_user_tenants.return_value = []

        removed = await deployment.members.remove_member(str(user.id), str(TENANT))

        assert removed == Ok({"status": "removed", "user_id": str(user.id)})
        deployment.adapter.sync_user.assert_awaited_once_with(
            user_id=user.id, data={"email": user.email, "status": "active", "left_tenant_id": str(TENANT)}
        )


async def test_not_found_from_the_service_is_never_a_404_of_its_own():
    """An unknown user and an outsider read the same: a member id leaks nothing about other tenants."""
    users = AsyncMock(spec=UserService)
    users.remove_user_from_tenant.return_value = Error(NotFound(message="User 'x' not found"))
    members = MemberService(users=users, roles=AsyncMock(spec=RoleService))

    assert await members.remove_member(str(uuid.uuid4()), str(TENANT)) == Error(
        Forbidden(message=MemberRefusal.NOT_IN_TENANT)
    )
