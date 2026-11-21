"""Keycloak's reconciliation pass: what "not yet delivered" means, the realm read, and push failures.

``KeycloakReconciliation`` runs on an in-memory realm (``FakeApi``), an in-memory
canonical side (``FakeState``, ``FakeLedger``) and a recording inbound sync. The
live suite (``tests/integration/test_keycloak_reconciliation.py``) runs the same
pass against the compose Keycloak and Postgres.
"""

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from expression import Error, Ok

from app.errors.identity import Conflict, NotFound, ProviderError
from app.services import keycloak_reconciliation as reconciliation_module
from app.services.adapters.base import LAST_ROLE_IN_TENANT, STATUS_CHANGED, SyncError
from app.services.adapters.keycloak import ROLE_ATTRIBUTE, Refusal
from app.services.identity_provisioning import DEFAULT_ROLE_NAME
from app.services.inbound_sync import RECONCILIATION_SOURCE
from app.services.keycloak_admin import KeycloakAdminUnavailable
from app.services.keycloak_events import (
    AssignmentChanged,
    ClientRoleDeleted,
    KeycloakAccount,
    MembershipChanged,
    TenantDeleted,
    TenantUpdated,
    UserDeleted,
    UserUpdated,
)
from app.services.keycloak_reconciliation import ALREADY_RUNNING, KeycloakReconciliation, _mark_of

PROVIDER_ID = uuid.uuid4()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass
class FakeApi:
    organizations_: list[dict] = field(default_factory=list)
    members: dict[str, list[dict]] = field(default_factory=dict)
    groups: dict[str, list[dict]] = field(default_factory=dict)
    group_members: dict[str, list[dict]] = field(default_factory=dict)
    users_: list[dict] = field(default_factory=list)
    roles: list[dict] = field(default_factory=list)
    # Accounts a paged list missed but that are there when asked by id.
    unlisted_users: list[dict] = field(default_factory=list)
    unavailable: bool = False

    async def organizations(self):
        if self.unavailable:
            raise KeycloakAdminUnavailable("admin API unreachable")
        return self.organizations_

    async def organization_members(self, organization_id):
        return self.members.get(organization_id, [])

    async def organization_groups(self, organization_id):
        return self.groups.get(organization_id, [])

    async def organization_group_members(self, organization_id, group_id):
        return self.group_members.get(group_id, [])

    async def users(self):
        return self.users_

    async def client_roles(self):
        return self.roles

    async def role_composites(self, role_id):
        return []

    async def find_organization(self, alias):
        return next((entry for entry in self.organizations_ if entry["alias"] == alias), None)

    async def find_user(self, username):
        return next((entry for entry in self.users_ if entry["username"] == username), None)

    async def get_user(self, user_id):
        return next((entry for entry in self.users_ + self.unlisted_users if entry["id"] == user_id), None)

    async def organizations_of(self, user_id):
        return frozenset(
            organization_id
            for organization_id, members in self.members.items()
            if user_id in {member["id"] for member in members}
        )


@dataclass
class FakeState:
    tenants_: list = field(default_factory=list)
    users_: list = field(default_factory=list)
    roles_: list = field(default_factory=list)
    permissions_: list = field(default_factory=list)
    links: list = field(default_factory=list)
    assignments: list = field(default_factory=list)

    async def tenants_linked_to(self, provider_id):
        return [tenant for tenant in self.tenants_ if tenant.external_org_id is not None]

    async def links_of(self, provider_id):
        return self.links

    async def users(self, user_ids):
        wanted = set(user_ids)
        return [user for user in self.users_ if user.id in wanted]

    async def tenants(self, tenant_ids):
        wanted = set(tenant_ids)
        return [tenant for tenant in self.tenants_ if tenant.id in wanted]

    async def roles(self):
        return self.roles_

    async def permissions(self):
        return self.permissions_

    async def role_permissions(self):
        return []

    async def assignments_in(self, tenant_ids):
        wanted = set(tenant_ids)
        return [entry for entry in self.assignments if entry.tenant_id in wanted]

    async def assignments_of(self, user_id, tenant_id):
        return [entry for entry in self.assignments if (entry.user_id, entry.tenant_id) == (user_id, tenant_id)]


@dataclass
class FakeLedger:
    """``pending_pushes``: ``marks`` is what is read first; ``later`` is what is marked when read again."""

    marks: dict[tuple[str, str], int] = field(default_factory=dict)
    later: dict[tuple[str, str], int] = field(default_factory=dict)
    cleared: list[tuple[str, str, int]] = field(default_factory=list)
    origin: list[bool] = field(default_factory=list)
    # The marks ``list_due`` answers (``PendingPushRepository.list_due``), and the intervals it was asked with.
    due: set[tuple[str, str]] = field(default_factory=set)
    due_asked: list[float] = field(default_factory=list)

    async def list_all(self):
        return [
            SimpleNamespace(kind=kind, key=key, seq=seq, snapshot=None)
            for (kind, key), seq in sorted({**self.marks, **self.later}.items())
        ]

    async def list_due(self, interval_seconds):
        self.due_asked.append(interval_seconds)
        return [mark for mark in await self.list_all() if (mark.kind, mark.key) in self.due]

    async def seqs(self, keys):
        current = {**self.marks, **self.later}
        return {key: current[key] for key in keys if key in current}

    async def clear(self, kind, key, seq):
        self.cleared.append((kind, key, seq))
        self.marks.pop((kind, key), None)
        return True

    async def count(self):
        return len(self.marks)

    async def provider_writes(self, on):
        self.origin.append(on)


class RecordingInbound:
    """Applies nothing; answers each change as applied to the subject ``answers`` names for it."""

    def __init__(self, answers=None):
        self.applied = []
        self.answers = answers or {}

    async def apply_keycloak_change(self, change, *, provider_id, keycloak_event_id, source):
        self.applied.append((change, source))
        subject_type, subject_id = self.answers.get(type(change), ("tenant", ""))
        return Ok({"status": "applied", "subject_type": subject_type, "subject_id": subject_id, "invalidations": []})


def _session():
    session = AsyncMock()
    session.begin_nested = AsyncMock(return_value=AsyncMock())
    return session


def _tenant(alias="acme"):
    return SimpleNamespace(
        id=uuid.uuid4(), name="Acme", domains=[], status=SimpleNamespace(value="active"), external_org_id=alias
    )


class FakeLinks:
    def __init__(self, linked=()):
        self.linked = set(linked)
        self.written = []

    async def has_link(self, user_id):
        return user_id in self.linked

    async def link_user(self, user_id, sub, email):
        self.written.append((user_id, sub, email))

    async def linked_elsewhere(self, tenant_id):
        return False


def _held(held=True):
    @asynccontextmanager
    async def hold():
        yield held

    return hold


def _pass(
    *,
    api=None,
    state=None,
    ledger=None,
    inbound=None,
    adapter=None,
    session=None,
    provider=True,
    events=None,
    links=None,
    hold_pass=None,
):
    providers = AsyncMock()
    providers.get_by_name.return_value = SimpleNamespace(id=PROVIDER_ID) if provider else None
    return KeycloakReconciliation(
        session=session or _session(),
        acquire_lock=AsyncMock(),
        hold_pass=hold_pass or _held(),
        api=api or FakeApi(),
        adapter=adapter or AsyncMock(),
        links=links or FakeLinks(),
        inbound=inbound or RecordingInbound(),
        state=state or FakeState(),
        ledger=ledger or FakeLedger(),
        pull_state=AsyncMock(),
        providers=providers,
        events=events or AsyncMock(),
    )


@pytest.mark.anyio
class TestNotYetDelivered:
    async def test_a_change_to_a_marked_entity_is_rolled_back(self):
        tenant = _tenant()
        session = _session()
        savepoint = AsyncMock()
        session.begin_nested.return_value = savepoint
        ledger = FakeLedger(marks={("tenant", str(tenant.id)): 7})
        api = FakeApi(organizations_=[{"id": "o1", "alias": "acme", "name": "Renamed", "enabled": True}])
        inbound = RecordingInbound({TenantUpdated: ("tenant", str(tenant.id))})
        adapter = AsyncMock()
        adapter.sync_tenant.return_value = Ok(None)

        result = await _pass(
            api=api,
            state=FakeState(tenants_=[tenant]),
            ledger=ledger,
            inbound=inbound,
            adapter=adapter,
            session=session,
        ).reconcile()

        assert result.is_ok()
        assert result.ok.stats["kept_pending"] == 1
        assert result.ok.stats["applied"] == 0
        savepoint.rollback.assert_awaited_once()
        savepoint.commit.assert_not_awaited()
        # Pushed instead, and its mark removed only up to the seq read.
        adapter.sync_tenant.assert_awaited_once_with(
            tenant_id=tenant.id, data={"name": "Acme", "self_provisioning_domains": [], "status": "active"}
        )
        assert ledger.cleared == [("tenant", str(tenant.id), 7)]

    async def test_a_change_to_an_entity_marked_during_the_pass_commits_nothing(self):
        tenant = _tenant()
        session = _session()
        ledger = FakeLedger(later={("tenant", str(tenant.id)): 9})
        ledger_marks_first = ledger.list_all

        async def first_read_sees_nothing():
            ledger.list_all = ledger_marks_first
            return []

        ledger.list_all = first_read_sees_nothing
        api = FakeApi(organizations_=[{"id": "o1", "alias": "acme", "name": "Renamed", "enabled": True}])
        inbound = RecordingInbound({TenantUpdated: ("tenant", str(tenant.id))})
        adapter = AsyncMock()

        result = await _pass(
            api=api,
            state=FakeState(tenants_=[tenant]),
            ledger=ledger,
            inbound=inbound,
            adapter=adapter,
            session=session,
        ).reconcile()

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        session.rollback.assert_awaited()
        # Nothing of the first half commits; the pending tenant is still pushed.
        adapter.sync_tenant.assert_awaited_once()

    async def test_a_change_to_an_unmarked_entity_is_applied(self):
        tenant = _tenant()
        session = _session()
        savepoint = AsyncMock()
        session.begin_nested.return_value = savepoint
        ledger = FakeLedger()
        api = FakeApi(organizations_=[{"id": "o1", "alias": "acme", "name": "Renamed", "enabled": True}])
        inbound = RecordingInbound({TenantUpdated: ("tenant", str(tenant.id))})

        result = await _pass(
            api=api, state=FakeState(tenants_=[tenant]), ledger=ledger, inbound=inbound, session=session
        ).reconcile()

        assert result.ok.stats["applied"] == 1
        savepoint.commit.assert_awaited_once()
        assert [change for change, _ in inbound.applied] == [
            TenantUpdated(alias="acme", name="Renamed", domains=(), enabled=True)
        ]
        assert {source for _, source in inbound.applied} == {RECONCILIATION_SOURCE}
        # The pass's writes are Keycloak's state: marked as such, then unmarked before the commit.
        assert ledger.origin == [True, False]

    @pytest.mark.parametrize(
        ("subject_type", "subject_id", "expected"),
        [
            ("tenant", "t", ("tenant", "t")),
            ("user", "u", ("user", "u")),
            ("permission", "p", ("permission", "p")),
            ("role", "r", ("role", "r")),
            ("membership", "u:t", ("membership", "u:t")),
            ("role_assignment", "u:t:r", ("membership", "u:t")),
            ("tenant", "", None),
            ("keycloak_event", "e", None),
        ],
    )
    async def test_the_mark_that_guards_each_subject(self, subject_type, subject_id, expected):
        assert _mark_of(subject_type, subject_id) == expected


@pytest.mark.anyio
class TestTheRealmRead:
    async def test_keycloak_unreadable_applies_and_pushes_nothing(self):
        session = _session()
        inbound = RecordingInbound()
        adapter = AsyncMock()
        ledger = FakeLedger(marks={("tenant", str(uuid.uuid4())): 1})

        result = await _pass(
            api=FakeApi(unavailable=True), inbound=inbound, adapter=adapter, ledger=ledger, session=session
        ).reconcile()

        assert result.is_error()
        assert isinstance(result.error, ProviderError)
        assert inbound.applied == []
        adapter.sync_tenant.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_a_pass_already_running(self):
        inbound = RecordingInbound()

        result = await _pass(hold_pass=_held(False), inbound=inbound).reconcile()

        assert result.error == Conflict(message=ALREADY_RUNNING)
        assert inbound.applied == []

    async def test_an_account_a_paged_list_missed_is_not_deleted(self):
        user_id = uuid.uuid4()
        state = FakeState(links=[SimpleNamespace(external_sub="kc-there", user_id=user_id)])
        api = FakeApi(unlisted_users=[{"id": "kc-there", "username": "there"}])
        inbound = RecordingInbound()

        await _pass(api=api, state=state, inbound=inbound).reconcile()

        assert [change for change, _ in inbound.applied if isinstance(change, UserDeleted)] == []

    async def test_no_provider_row(self):
        result = await _pass(provider=False).reconcile()

        assert isinstance(result.error, NotFound)

    async def test_the_lock_not_taken(self):
        reconciliation = _pass()
        reconciliation._pull.lock.side_effect = RuntimeError("lock")

        result = await reconciliation.reconcile()

        assert isinstance(result.error, ProviderError)

    async def test_what_each_difference_reads_as(self):
        """Gone organization, gone linked account, gone role, member with no group, member removed, extra role."""
        member_role = SimpleNamespace(id=uuid.uuid4(), name=DEFAULT_ROLE_NAME, tenant_id=None, description="")
        admin = SimpleNamespace(id=uuid.uuid4(), name="admin", tenant_id=None, description="")
        gone_role = SimpleNamespace(id=uuid.uuid4(), name="gone", tenant_id=None, description="")
        kept = _tenant("kept")
        deleted = _tenant("deleted")
        grouped, ungrouped, removed, departed = (uuid.uuid4() for _ in range(4))
        state = FakeState(
            tenants_=[kept, deleted],
            roles_=[member_role, admin, gone_role],
            links=[
                SimpleNamespace(external_sub="kc-grouped", user_id=grouped),
                SimpleNamespace(external_sub="kc-ungrouped", user_id=ungrouped),
                SimpleNamespace(external_sub="kc-removed", user_id=removed),
                SimpleNamespace(external_sub="kc-departed", user_id=departed),
            ],
            assignments=[
                SimpleNamespace(user_id=grouped, tenant_id=kept.id, role_id=member_role.id),
                SimpleNamespace(user_id=ungrouped, tenant_id=kept.id, role_id=admin.id),
                SimpleNamespace(user_id=removed, tenant_id=kept.id, role_id=admin.id),
            ],
        )
        accounts = [
            {"id": f"kc-{name}", "username": name, "enabled": True, "emailVerified": False, "email": f"{name}@x.test"}
            for name in ("grouped", "ungrouped", "removed")
        ]
        api = FakeApi(
            organizations_=[{"id": "o-kept", "alias": "kept", "name": "Kept", "enabled": True}],
            members={"o-kept": [{"id": "kc-grouped"}, {"id": "kc-ungrouped"}]},
            groups={"o-kept": [{"id": "g-admin", "name": "admin", "attributes": {ROLE_ATTRIBUTE: [str(admin.id)]}}]},
            group_members={"g-admin": [{"id": "kc-grouped"}]},
            users_=accounts,
            roles=[
                {"id": "r-member", "name": DEFAULT_ROLE_NAME, "attributes": {}},
                {"id": "r-admin", "name": "admin", "attributes": {ROLE_ATTRIBUTE: [str(admin.id)]}},
            ],
        )
        inbound = RecordingInbound()

        await _pass(api=api, state=state, inbound=inbound).reconcile()

        changes = [change for change, _ in inbound.applied]
        by_kind = {}
        for change in changes:
            by_kind.setdefault(type(change), []).append(change)

        def account(name):
            return KeycloakAccount(id=f"kc-{name}", username=name, admin_named=False)

        assert by_kind[TenantDeleted] == [TenantDeleted(alias="deleted")]
        assert by_kind[UserDeleted] == [UserDeleted(KeycloakAccount(id="kc-departed", username=None))]
        assert by_kind[ClientRoleDeleted][0].role.role_id == str(gone_role.id)
        # Unverified emails are not taken.
        assert {change.email for change in by_kind[UserUpdated]} == {None}
        assert by_kind[MembershipChanged] == [
            MembershipChanged(alias="kept", account=account("ungrouped"), added=True),
            MembershipChanged(alias="kept", account=account("removed"), added=False),
        ]
        assert by_kind[AssignmentChanged] == [
            AssignmentChanged("kept", account("grouped"), DEFAULT_ROLE_NAME, str(member_role.id), added=False),
            AssignmentChanged("kept", account("grouped"), "admin", str(admin.id), added=True),
            AssignmentChanged("kept", account("ungrouped"), "admin", str(admin.id), added=False),
        ]


@pytest.mark.anyio
class TestPushPending:
    """The push retry's turn: the due marks alone, nothing of the realm read."""

    async def test_only_the_due_marks_are_pushed(self):
        due_tenant, later_tenant = _tenant(), _tenant()
        ledger = FakeLedger(
            marks={("tenant", str(due_tenant.id)): 1, ("tenant", str(later_tenant.id)): 2},
            due={("tenant", str(due_tenant.id))},
        )
        adapter = AsyncMock()
        adapter.sync_tenant.return_value = Ok(None)
        inbound = RecordingInbound()

        result = await _pass(
            state=FakeState(tenants_=[due_tenant, later_tenant]), ledger=ledger, adapter=adapter, inbound=inbound
        ).push_pending(60.0)

        assert result.ok == {"pushed": 1, "push_failed": 0, "pending": 1}
        assert ledger.due_asked == [60.0]
        assert ledger.cleared == [("tenant", str(due_tenant.id), 1)]
        assert [call.kwargs["tenant_id"] for call in adapter.sync_tenant.await_args_list] == [due_tenant.id]
        assert inbound.applied == []

    async def test_a_turn_while_another_pushes(self):
        ledger = FakeLedger(marks={("tenant", str(uuid.uuid4())): 1}, due={("tenant", "x")})

        result = await _pass(hold_pass=_held(False), ledger=ledger).push_pending(60.0)

        assert isinstance(result.error, Conflict)
        assert ledger.due_asked == []

    async def test_no_provider_row(self):
        assert isinstance((await _pass(provider=False).push_pending(60.0)).error, NotFound)


@pytest.mark.anyio
class TestPushes:
    async def test_a_failed_push_keeps_its_mark_and_is_recorded(self):
        user = SimpleNamespace(
            id=uuid.uuid4(), email="a@x.test", status=SimpleNamespace(value="inactive"), given_name="A", family_name="B"
        )
        ledger = FakeLedger(marks={("user", str(user.id)): 3})
        adapter = AsyncMock()
        adapter.sync_user.return_value = Error(SyncError(message="boom", operation="sync_user", context={}))
        events = AsyncMock()

        result = await _pass(state=FakeState(users_=[user]), ledger=ledger, adapter=adapter, events=events).reconcile()

        assert result.ok.stats["push_failed"] == 1
        assert result.ok.stats["pending"] == 1
        assert ledger.cleared == []
        recorded = events.create_now.await_args.args[0]
        assert (recorded.subject_type, recorded.subject_id, recorded.detail["operation"]) == (
            "user",
            str(user.id),
            "sync_user",
        )

    async def test_a_user_is_pushed_profile_first_then_status(self):
        user = SimpleNamespace(
            id=uuid.uuid4(), email="a@x.test", status=SimpleNamespace(value="inactive"), given_name="A", family_name="B"
        )
        ledger = FakeLedger(marks={("user", str(user.id)): 3})
        adapter = AsyncMock()
        adapter.sync_user.return_value = Ok(None)

        await _pass(
            api=FakeApi(users_=[{"id": "kc", "username": str(user.id)}]),
            state=FakeState(users_=[user]),
            ledger=ledger,
            adapter=adapter,
            links=FakeLinks(linked={user.id}),
        ).reconcile()

        data = {"email": "a@x.test", "status": "inactive", "given_name": "A", "family_name": "B"}
        assert [entry.kwargs for entry in adapter.sync_user.await_args_list] == [
            {"user_id": user.id, "data": data},
            {"user_id": user.id, "data": {**data, STATUS_CHANGED: True}},
        ]
        assert ledger.cleared == [("user", str(user.id), 3)]

    async def test_an_account_the_push_creates_is_linked(self):
        user = SimpleNamespace(
            id=uuid.uuid4(), email="a@x.test", status=SimpleNamespace(value="active"), given_name="A", family_name="B"
        )
        api = FakeApi()
        adapter = AsyncMock()

        async def create(*, user_id, data):
            if not api.users_:
                api.users_.append({"id": "kc-new", "username": str(user_id)})
            return Ok(None)

        adapter.sync_user.side_effect = create
        links = FakeLinks()

        await _pass(
            api=api,
            state=FakeState(users_=[user]),
            ledger=FakeLedger(marks={("user", str(user.id)): 1}),
            adapter=adapter,
            links=links,
        ).reconcile()

        assert links.written == [(user.id, "kc-new", "a@x.test")]

    async def test_an_account_already_under_the_canonical_id_is_not_linked(self):
        """A username is not proof: an account someone else made under the canonical id stays unlinked."""
        user = SimpleNamespace(
            id=uuid.uuid4(), email="a@x.test", status=SimpleNamespace(value="active"), given_name="A", family_name="B"
        )
        api = FakeApi(users_=[{"id": "kc-other", "username": str(user.id)}])
        adapter = AsyncMock()
        adapter.sync_user.return_value = Ok(None)
        links = FakeLinks()

        await _pass(
            api=api,
            state=FakeState(users_=[user]),
            ledger=FakeLedger(marks={("user", str(user.id)): 1}),
            adapter=adapter,
            links=links,
        ).reconcile()

        assert links.written == []

    async def test_an_unlinked_account_named_after_a_canonical_user_is_not_that_user(self):
        user = SimpleNamespace(id=uuid.uuid4())
        api = FakeApi(
            users_=[
                {
                    "id": "kc-squatter",
                    "username": str(user.id),
                    "enabled": False,
                    "emailVerified": True,
                    "email": "s@x.test",
                }
            ]
        )
        inbound = RecordingInbound()

        await _pass(api=api, state=FakeState(users_=[user]), inbound=inbound).reconcile()

        assert [change for change, _ in inbound.applied if isinstance(change, UserUpdated)] == []

    async def test_a_tenant_linked_to_another_provider_has_nothing_to_push(self):
        tenant = _tenant(alias=None)
        ledger = FakeLedger(marks={("tenant", str(tenant.id)): 2})
        adapter = AsyncMock()
        adapter.sync_tenant.return_value = Error(
            SyncError(
                message="elsewhere", operation="sync_tenant", context={"refusal": Refusal.TENANT_LINKED_ELSEWHERE}
            )
        )

        await _pass(state=FakeState(tenants_=[tenant]), ledger=ledger, adapter=adapter).reconcile()

        assert ledger.cleared == [("tenant", str(tenant.id), 2)]

    async def test_a_membership_with_no_role_left_is_ended(self):
        user_id, tenant = uuid.uuid4(), _tenant()
        ledger = FakeLedger(marks={("membership", f"{user_id}:{tenant.id}"): 4})
        adapter = AsyncMock()
        adapter.delete_role_assignment.return_value = Ok(None)

        await _pass(state=FakeState(tenants_=[tenant]), ledger=ledger, adapter=adapter).reconcile()

        assert adapter.delete_role_assignment.await_args.kwargs["data"] == {LAST_ROLE_IN_TENANT: True}
        assert ledger.cleared == [("membership", f"{user_id}:{tenant.id}", 4)]

    async def test_ending_a_membership_of_a_user_keycloak_lacks_is_done(self):
        user_id, tenant = uuid.uuid4(), _tenant()
        ledger = FakeLedger(marks={("membership", f"{user_id}:{tenant.id}"): 4})
        adapter = AsyncMock()
        adapter.delete_role_assignment.return_value = Error(
            SyncError(message="no account", operation="x", context={"refusal": Refusal.USER_NOT_IN_KEYCLOAK})
        )

        await _pass(state=FakeState(tenants_=[tenant]), ledger=ledger, adapter=adapter).reconcile()

        assert ledger.cleared == [("membership", f"{user_id}:{tenant.id}", 4)]

    async def test_pushes_run_in_dependency_order(self):
        ids = {kind: str(uuid.uuid4()) for kind in ("tenant", "permission", "role", "user")}
        ledger = FakeLedger(
            marks={
                ("membership", f"{ids['user']}:{ids['tenant']}"): 1,
                ("user", ids["user"]): 2,
                ("role", ids["role"]): 3,
                ("permission", ids["permission"]): 4,
                ("tenant", ids["tenant"]): 5,
            }
        )
        adapter = AsyncMock()
        for name in ("delete_tenant", "delete_permission", "delete_role", "delete_user", "delete_role_assignment"):
            getattr(adapter, name).return_value = Ok(None)

        await _pass(ledger=ledger, adapter=adapter).reconcile()

        assert [kind for kind, _, _ in ledger.cleared] == ["tenant", "permission", "role", "user", "membership"]


class _AlwaysLock:
    async def __aenter__(self) -> bool:  # pragma: no cover - test helper
        return True

    async def __aexit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - test helper
        return None


@pytest.fixture(autouse=True)
def _mock_membership_lock(monkeypatch):
    monkeypatch.setattr(reconciliation_module, "hold_keycloak_membership_lock", lambda *_args, **_kwargs: _AlwaysLock())
    yield


@pytest.mark.anyio
async def test_membership_commit_occurs_under_lock(monkeypatch):
    class RecordingLock:
        def __init__(self) -> None:
            self.active = False
            self.commit_seen = False
            self.exited = False

        async def __aenter__(self) -> bool:
            self.active = True
            return True

        async def __aexit__(self, exc_type, exc, tb) -> None:
            assert self.commit_seen
            self.active = False
            self.exited = True

    lock = RecordingLock()
    monkeypatch.setattr(reconciliation_module, "hold_keycloak_membership_lock", lambda *_args, **_kwargs: lock)

    session = AsyncMock()

    async def commit() -> None:
        if not lock.active:
            raise AssertionError("commit occurred after lock release")
        lock.commit_seen = True

    session.commit.side_effect = commit
    session.rollback = AsyncMock()

    ledger = AsyncMock()
    ledger.clear = AsyncMock()

    reconciliation = KeycloakReconciliation.__new__(KeycloakReconciliation)
    reconciliation._session = session
    reconciliation._ledger = ledger
    reconciliation._push_membership = AsyncMock(return_value=None)
    reconciliation._failed = AsyncMock()

    stats = {"pushed": 0}

    await reconciliation._push_membership_guarded(uuid.uuid4(), "run", uuid.uuid4(), uuid.uuid4(), 1, {}, stats)

    assert lock.commit_seen is True
    assert lock.exited is True
    ledger.clear.assert_awaited()
