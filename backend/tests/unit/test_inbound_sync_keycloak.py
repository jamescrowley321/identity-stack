"""InboundSyncService.apply_keycloak_change: how each change the Keycloak pull reads lands in the canonical model.

Repositories are mocks; the live suite (``tests/integration/test_keycloak_event_pull.py``)
runs the same handlers against Postgres.
"""

import uuid
from unittest.mock import AsyncMock

from app.errors.identity import Conflict, NotFound
from app.models.identity.role import Permission, Role
from app.models.identity.sync_event import SyncEventVerb
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.base import RepositoryConflictError
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.inbound_sync import InboundSyncService, KeycloakPullRepositories
from app.services.keycloak_events import (
    AssignmentChanged,
    ClientRoleRef,
    ClientRoleSaved,
    KeycloakAccount,
    MembershipChanged,
    TenantUpdated,
    UserUpdated,
)

PROVIDER_ID = uuid.uuid4()
EVENT_ID = "keycloak-event-1"
ACCOUNT = KeycloakAccount(id="kc-user", username="bob")


class Canonical:
    """The service on mock repositories, with one tenant linked to ``acme`` and one user linked to ``kc-user``."""

    def __init__(self) -> None:
        self.tenant = Tenant(
            id=uuid.uuid4(), name="Acme", external_org_id="acme", domains=[], status=TenantStatus.active
        )
        self.user = User(id=uuid.uuid4(), email="bob@acme.test", user_name="bob", status=UserStatus.active)
        self.users = AsyncMock(spec=UserRepository)
        self.users.get.return_value = self.user
        self.links = AsyncMock(spec=IdPLinkRepository)
        self.links.get_by_provider_and_sub.return_value = IdPLink(
            user_id=self.user.id, provider_id=PROVIDER_ID, external_sub="kc-user"
        )
        self.events = AsyncMock(spec=SyncEventRepository)
        self.tenants = AsyncMock(spec=TenantRepository)
        self.tenants.list_by_external_org_id.return_value = [self.tenant]
        self.roles = AsyncMock(spec=RoleRepository)
        self.permissions = AsyncMock(spec=PermissionRepository)
        self.assignments = AsyncMock(spec=UserTenantRoleRepository)
        self.service = InboundSyncService(
            user_repository=self.users,
            idp_link_repository=self.links,
            provider_repository=AsyncMock(spec=ProviderRepository),
            sync_event_repository=self.events,
            keycloak_repositories=KeycloakPullRepositories(
                tenant=self.tenants, role=self.roles, permission=self.permissions, assignment=self.assignments
            ),
        )

    async def apply(self, change):
        return await self.service.apply_keycloak_change(change, provider_id=PROVIDER_ID, keycloak_event_id=EVENT_ID)

    def recorded(self) -> list[tuple[SyncEventVerb, str]]:
        return [(call.args[0].verb, call.args[0].subject_type) for call in self.events.create.call_args_list]


async def test_without_the_keycloak_repositories_nothing_is_applied():
    service = InboundSyncService(
        user_repository=AsyncMock(spec=UserRepository),
        idp_link_repository=AsyncMock(spec=IdPLinkRepository),
        provider_repository=AsyncMock(spec=ProviderRepository),
    )

    result = await service.apply_keycloak_change(
        TenantUpdated("acme", "A", None, None), provider_id=PROVIDER_ID, keycloak_event_id=EVENT_ID
    )

    assert result.is_error()


async def test_an_organization_no_tenant_is_linked_to_is_skipped_and_recorded():
    canonical = Canonical()
    canonical.tenants.list_by_external_org_id.return_value = []

    result = await canonical.apply(TenantUpdated("elsewhere", "Elsewhere", None, False))

    assert result.ok["status"] == "skipped"
    assert canonical.recorded() == [(SyncEventVerb.skipped, "tenant")]
    canonical.tenants.update.assert_not_called()


async def test_a_change_already_in_the_canonical_model_writes_nothing_and_records_nothing():
    canonical = Canonical()

    result = await canonical.apply(TenantUpdated("acme", "Acme", (), True))

    assert (result.ok["status"], canonical.recorded()) == ("unchanged", [])
    canonical.tenants.update.assert_not_called()


async def test_a_disabled_account_deactivates_the_user_and_names_it_for_invalidation():
    canonical = Canonical()

    result = await canonical.apply(UserUpdated(ACCOUNT, enabled=False))

    assert canonical.user.status == UserStatus.inactive
    assert result.ok["invalidations"] == [
        {"entity_type": "user", "entity_id": str(canonical.user.id), "operation": "deactivate"}
    ]
    assert canonical.recorded() == [(SyncEventVerb.updated, "user")]


async def test_an_email_another_user_holds_is_an_error():
    canonical = Canonical()
    canonical.users.update.side_effect = RepositoryConflictError("duplicate")

    result = await canonical.apply(UserUpdated(ACCOUNT, email="alice@acme.test"))

    assert isinstance(result.error, Conflict)


async def test_an_account_linked_to_another_canonical_user_by_username_is_not_that_user():
    """A username that is a canonical id names that user only when it has no other Keycloak link."""
    canonical = Canonical()
    canonical.links.get_by_provider_and_sub.return_value = None
    canonical.links.get_by_user.return_value = [
        IdPLink(user_id=canonical.user.id, provider_id=PROVIDER_ID, external_sub="another-account")
    ]

    result = await canonical.apply(
        UserUpdated(KeycloakAccount(id="kc-user", username=str(canonical.user.id)), enabled=False)
    )

    assert (result.ok["status"], canonical.user.status) == ("skipped", UserStatus.active)


async def test_a_member_added_holds_the_default_role_unless_they_hold_one():
    canonical = Canonical()
    member = Role(id=uuid.uuid4(), name="member")
    canonical.roles.get_by_name.return_value = member
    canonical.assignments.list_by_user_tenant.return_value = []

    added = await canonical.apply(MembershipChanged("acme", ACCOUNT, added=True))
    canonical.assignments.list_by_user_tenant.return_value = [object()]
    again = await canonical.apply(MembershipChanged("acme", ACCOUNT, added=True))

    (created,) = [call.args[0] for call in canonical.assignments.create.call_args_list]
    assert (created.user_id, created.tenant_id, created.role_id) == (canonical.user.id, canonical.tenant.id, member.id)
    assert (added.ok["status"], again.ok["status"]) == ("applied", "unchanged")


async def test_a_member_added_without_a_default_role_is_an_error():
    canonical = Canonical()
    canonical.roles.get_by_name.return_value = None
    canonical.assignments.list_by_user_tenant.return_value = []

    result = await canonical.apply(MembershipChanged("acme", ACCOUNT, added=True))

    assert isinstance(result.error, NotFound)


async def test_a_role_group_of_another_tenants_role_assigns_nothing():
    canonical = Canonical()
    canonical.roles.get.return_value = Role(id=uuid.uuid4(), name="editor", tenant_id=uuid.uuid4())

    result = await canonical.apply(AssignmentChanged("acme", ACCOUNT, "editor", role_id=str(uuid.uuid4()), added=True))

    assert result.ok["status"] == "skipped"
    canonical.assignments.create.assert_not_called()


async def test_a_tenant_role_is_named_with_its_tenant():
    canonical = Canonical()
    canonical.roles.get_by_name.return_value = None
    canonical.tenants.get.return_value = canonical.tenant
    name = f"editor@{canonical.tenant.id}"

    result = await canonical.apply(
        ClientRoleSaved(name=name, description="Edits", previous=None, current=ClientRoleRef(name, None, None, False))
    )

    (row,) = [call.args[0] for call in canonical.roles.create.call_args_list]
    assert (row.name, row.tenant_id, row.description, result.ok["status"]) == (
        "editor",
        canonical.tenant.id,
        "Edits",
        "applied",
    )


async def test_a_permission_renamed_is_found_by_what_it_was():
    canonical = Canonical()
    held = Permission(id=uuid.uuid4(), name="documents:read", description="")
    canonical.permissions.get_by_name.side_effect = lambda name: held if name == "documents:read" else None

    await canonical.apply(
        ClientRoleSaved(
            name="documents:list",
            description=None,
            previous=ClientRoleRef("documents:read", None, None, True),
            current=ClientRoleRef("documents:list", None, None, True),
        )
    )

    assert held.name == "documents:list"
    canonical.permissions.create.assert_not_called()
