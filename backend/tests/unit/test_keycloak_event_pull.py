"""The Keycloak event pull's ordering, skipping, unknown events and failure handling.

``KeycloakPullService`` and the shipped ``KeycloakEventReader`` run on an
in-memory realm (``FakeRealm``: the event stores and the reads the reader makes)
and an in-memory canonical side (``FakeInbound``, ``FakeStates``). The live suite
(``tests/integration/test_keycloak_event_pull.py``) runs the same service against
the compose Keycloak and Postgres.
"""

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from expression import Error, Ok

from app.errors.identity import Conflict, NotFound, ProviderError
from app.services.keycloak_admin import EventStream, KeycloakAdminError, KeycloakAdminSettings
from app.services.keycloak_events import (
    ClientRoleDeleted,
    ClientRoleSaved,
    CompositesChanged,
    Ignored,
    KeycloakEventReader,
    MembershipChanged,
    RealmSnapshot,
    TenantDeleted,
    TenantUpdated,
    UserDeleted,
    UserUpdated,
)
from app.services.keycloak_pull import OVERLAP_MS, KeycloakPullService

SERVICE_ACCOUNT = "service-account-id"
CONSOLE_ADMIN = "console-admin-id"
API_CLIENT = "api-client-uuid"
ISSUER = "http://keycloak.localhost/realms/identity-stack"
PROVIDER_ID = uuid.uuid4()
START = 1_800_000_000_000


def admin_event(
    time: int, resource: str, operation: str, path: str, representation=None, *, author=CONSOLE_ADMIN, details=None
) -> dict:
    event = {
        "id": str(uuid.uuid4()),
        "time": time,
        "operationType": operation,
        "resourceType": resource,
        "resourcePath": path,
        "authDetails": {"userId": author},
    }
    if representation is not None:
        event["representation"] = json.dumps(representation)
    if details is not None:
        event["details"] = details
    return event


def user_event(time: int, kind: str, user_id: str = "kc-user", *, client_id: str = "identity-stack-spa") -> dict:
    return {"id": str(uuid.uuid4()), "time": time, "type": kind, "userId": user_id, "clientId": client_id}


def org_update(time: int, alias: str = "acme", name: str = "Acme", **kwargs) -> dict:
    return admin_event(
        time,
        "ORGANIZATION",
        "UPDATE",
        "organizations/org-1",
        {"id": "org-1", "alias": alias, "name": name, "enabled": True},
        **kwargs,
    )


@dataclass
class FakeRealm:
    """The Admin API reads the pull makes, over in-memory event stores."""

    admin: list[dict] = field(default_factory=list)
    user: list[dict] = field(default_factory=list)
    users: dict[str, dict] = field(default_factory=dict)
    failure: KeycloakAdminError | None = None
    reads: list[tuple] = field(default_factory=list)
    settings: KeycloakAdminSettings = field(
        default_factory=lambda: KeycloakAdminSettings(ISSUER, "identity-stack-admin", "secret", "identity-stack-api")
    )

    async def events(self, stream, *, date_from, first, max_results, newest=False):
        self.reads.append((stream, date_from, first, max_results, newest))
        store = self.admin if stream is EventStream.ADMIN else self.user
        ordered = sorted(
            (event for event in store if event["time"] >= date_from), key=lambda event: event["time"], reverse=newest
        )
        return ordered[first : first + max_results]

    async def service_account_id(self):
        return SERVICE_ACCOUNT

    async def api_client_uuid(self):
        return API_CLIENT

    async def list_organizations(self):
        return [{"id": "org-1", "alias": "acme"}]

    async def client_roles(self):
        return [{"id": "role-1", "name": "documents:read", "attributes": {}}]

    async def get_user(self, user_id):
        if self.failure is not None:
            raise self.failure
        return self.users.get(user_id)

    async def find_user(self, username):
        return next((user for user in self.users.values() if user["username"] == username), None)

    async def get_organization(self, organization_id):
        return None

    async def organization_groups(self, organization_id):
        return []


@dataclass
class FakeInbound:
    """The canonical side: records each change; answers an error for the changes in ``refuse``."""

    applied: list = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    refuse: set[str] = field(default_factory=set)

    async def apply_keycloak_change(self, change, *, provider_id, keycloak_event_id):
        if keycloak_event_id in self.refuse:
            return Error(Conflict(message="refused"))
        self.applied.append(change)
        return Ok({"status": "applied", "subject_type": "x", "subject_id": "y", "invalidations": []})

    async def record_keycloak_failure(self, *, provider_id, keycloak_event_id, change, message):
        self.failures.append((keycloak_event_id, change))
        return Ok(None)


class FakeSavepoint:
    def __init__(self, session):
        self._session = session

    async def commit(self):
        self._session.savepoints.append("commit")

    async def rollback(self):
        self._session.savepoints.append("rollback")


@dataclass
class FakeSession:
    savepoints: list[str] = field(default_factory=list)
    rollbacks: int = 0

    async def begin_nested(self):
        return FakeSavepoint(self)

    async def rollback(self):
        self.rollbacks += 1


@dataclass
class FakeStates:
    """The state row: committed only by ``commit``, as the transaction would."""

    committed: dict | None = None
    locked: bool = True
    pending: SimpleNamespace | None = None
    commits: int = 0

    async def try_lock(self):
        return self.locked

    async def get_realm(self, realm):
        if self.committed is None:
            return None
        self.pending = SimpleNamespace(**json.loads(json.dumps(self.committed)))
        return self.pending

    async def start(self, realm, *, admin, user, organizations, client_roles):
        self.pending = SimpleNamespace(
            admin_cursor=admin[0],
            admin_seen=admin[1],
            user_cursor=user[0],
            user_seen=user[1],
            organizations=organizations,
            client_roles=client_roles,
        )

    async def update(self, state):
        self.pending = state

    async def commit(self):
        self.commits += 1
        self.committed = json.loads(json.dumps(vars(self.pending)))


def started(cursor: int = START, *, seen: dict | None = None) -> dict:
    return {
        "admin_cursor": cursor,
        "admin_seen": seen or {},
        "user_cursor": cursor,
        "user_seen": {},
        "organizations": {"org-1": "acme"},
        "client_roles": {"role-1": {"name": "documents:read", "role_id": None, "permission_id": None}},
    }


def service(
    realm: FakeRealm,
    inbound: FakeInbound,
    states: FakeStates,
    session: FakeSession | None = None,
    *,
    page_size: int = 100,
):
    providers = SimpleNamespace(get_by_name=_async(SimpleNamespace(id=PROVIDER_ID)))
    return KeycloakPullService(
        session=session or FakeSession(),
        api=realm,
        reader=KeycloakEventReader(realm),
        inbound=inbound,
        state_repository=states,
        provider_repository=providers,
        page_size=page_size,
    )


def _async(value):
    async def answer(*_args, **_kwargs):
        return value

    return answer


# --- ordering --------------------------------------------------------------------------


class TestOrdering:
    async def test_events_of_both_stores_are_applied_oldest_first(self):
        realm = FakeRealm(users={"kc-user": {"id": "kc-user", "username": "u", "email": "u@x.test"}})
        realm.admin = [org_update(START + 30, name="Third"), org_update(START + 10, name="First")]
        realm.user = [user_event(START + 20, "DELETE_ACCOUNT"), user_event(START + 40, "UPDATE_PROFILE")]
        inbound, states = FakeInbound(), FakeStates(committed=started())

        assert (await service(realm, inbound, states).poll()).is_ok()

        assert [type(change).__name__ for change in inbound.applied] == [
            "TenantUpdated",
            "UserDeleted",
            "TenantUpdated",
            "UserUpdated",
        ]
        assert [change.name for change in inbound.applied if isinstance(change, TenantUpdated)] == ["First", "Third"]
        assert (states.committed["admin_cursor"], states.committed["user_cursor"]) == (START + 30, START + 40)

    async def test_full_pages_are_applied_a_batch_at_a_time_in_order(self):
        realm = FakeRealm()
        realm.admin = [org_update(START + step, name=f"n{step}") for step in range(1, 6)]
        realm.user = [user_event(START + 3, "LOGIN")]
        inbound, states = FakeInbound(), FakeStates(committed=started())

        stats = (await service(realm, inbound, states, page_size=2).poll()).ok

        assert [change.name for change in inbound.applied] == ["n1", "n2", "n3", "n4", "n5"]
        assert (stats["applied"], stats["ignored"]) == (5, 1)
        assert states.commits == 3

    async def test_an_event_already_applied_at_the_cursor_is_not_applied_again(self):
        applied, new = org_update(START, name="Applied"), org_update(START, name="New")
        late = org_update(START - OVERLAP_MS + 1, name="Late")
        realm = FakeRealm(admin=[applied, new, late, org_update(START - OVERLAP_MS - 1, name="Old")])
        inbound = FakeInbound()
        states = FakeStates(committed=started(seen={applied["id"]: START}))

        await service(realm, inbound, states).poll()

        assert [change.name for change in inbound.applied] == ["Late", "New"]
        assert set(states.committed["admin_seen"]) == {applied["id"], new["id"], late["id"]}

    async def test_a_restart_applies_nothing_already_committed(self):
        realm = FakeRealm(admin=[org_update(START + 1), org_update(START + 2)])
        inbound, states = FakeInbound(), FakeStates(committed=started())
        await service(realm, inbound, states).poll()

        again = FakeInbound()
        stats = (await service(realm, again, states).poll()).ok

        assert (len(inbound.applied), again.applied, stats["applied"]) == (2, [], 0)


# --- skipping and unknown events ----------------------------------------------------------


class TestSkipping:
    async def test_the_backends_own_admin_events_and_its_clients_user_events_change_nothing(self):
        realm = FakeRealm(users={"kc-user": {"id": "kc-user", "username": "u"}})
        realm.admin = [org_update(START + 1, author=SERVICE_ACCOUNT)]
        realm.user = [user_event(START + 2, "UPDATE_PROFILE", client_id="identity-stack-admin")]
        inbound, states = FakeInbound(), FakeStates(committed=started())

        stats = (await service(realm, inbound, states).poll()).ok

        assert (inbound.applied, stats["own"]) == ([], 2)
        assert states.committed["admin_cursor"] == START + 1

    async def test_the_snapshot_follows_the_backends_own_changes(self):
        """A role the backend creates and a console admin then deletes is still named by the delete."""
        created = admin_event(
            START + 1,
            "CLIENT_ROLE",
            "CREATE",
            f"clients/{API_CLIENT}/roles/reports:read",
            {"id": "role-2", "name": "reports:read"},
            author=SERVICE_ACCOUNT,
        )
        deleted = admin_event(START + 2, "CLIENT_ROLE", "DELETE", "roles-by-id/role-2")
        realm = FakeRealm(admin=[created, deleted])
        inbound = FakeInbound()

        await service(realm, inbound, FakeStates(committed=started())).poll()

        assert inbound.applied == [ClientRoleDeleted(role=inbound.applied[0].role)]
        assert (inbound.applied[0].role.name, inbound.applied[0].role.is_permission) == ("reports:read", True)

    @pytest.mark.parametrize(
        "event",
        [
            admin_event(START + 1, "CLIENT", "UPDATE", "clients/x", {"clientId": "x"}),
            admin_event(START + 1, "REALM_ROLE", "CREATE", "roles/x", {"id": "r", "name": "x"}),
            admin_event(START + 1, "CLIENT_ROLE", "CREATE", "clients/other/roles/x", {"id": "r", "name": "x"}),
            admin_event(START + 1, "CLIENT_ROLE", "DELETE", "roles-by-id/not-the-api-clients"),
            admin_event(START + 1, "ORGANIZATION", "DELETE", "organizations/unknown-org"),
            admin_event(START + 1, "USER", "CREATE", "users/u", {"username": "u"}),
            admin_event(START + 1, "USER", "ACTION", "users/u/reset-password"),
            user_event(START + 1, "LOGIN"),
        ],
        ids=[
            "a client",
            "a realm role",
            "another client's role",
            "an unknown role id",
            "an unknown organization",
            "a user created in Keycloak",
            "a password reset",
            "a login",
        ],
    )
    async def test_an_unmapped_event_is_passed_and_changes_nothing(self, event):
        realm = FakeRealm()
        (realm.user if "type" in event else realm.admin).append(event)
        inbound, states = FakeInbound(), FakeStates(committed=started())

        stats = (await service(realm, inbound, states).poll()).ok

        assert (inbound.applied, inbound.failures, stats["ignored"]) == ([], [], 1)
        assert max(states.committed["admin_cursor"], states.committed["user_cursor"]) == START + 1


# --- failures ----------------------------------------------------------------------------


class TestFailures:
    async def test_a_change_the_canonical_side_refuses_is_rolled_back_recorded_and_passed(self):
        refused, next_one = org_update(START + 1, name="Refused"), org_update(START + 2, name="Next")
        realm = FakeRealm(admin=[refused, next_one])
        inbound, session, states = FakeInbound(refuse={refused["id"]}), FakeSession(), FakeStates(committed=started())

        stats = (await service(realm, inbound, states, session).poll()).ok

        assert inbound.failures == [(refused["id"], "TenantUpdated")]
        assert [change.name for change in inbound.applied] == ["Next"]
        assert session.savepoints == ["rollback", "commit"]
        assert (stats["failed"], stats["applied"], states.committed["admin_cursor"]) == (1, 1, START + 2)

    @pytest.mark.parametrize("status", [None, 500, 503, 401, 429])
    async def test_keycloak_unavailable_stops_the_poll_without_moving_the_cursor(self, status):
        realm = FakeRealm(failure=KeycloakAdminError("down", status=status))
        realm.admin = [org_update(START + 1)]
        realm.user = [user_event(START + 2, "UPDATE_PROFILE")]
        inbound, session, states = FakeInbound(), FakeSession(), FakeStates(committed=started())

        result = await service(realm, inbound, states, session).poll()

        assert isinstance(result.error, ProviderError)
        assert (states.commits, states.committed["admin_cursor"], session.rollbacks) == (0, START, 1)

    async def test_an_event_keycloak_refuses_to_explain_is_recorded_and_passed(self):
        refused = user_event(START + 1, "UPDATE_PROFILE")
        realm = FakeRealm(failure=KeycloakAdminError("refused", status=400), admin=[org_update(START + 2)])
        realm.user = [refused]
        inbound, states = FakeInbound(), FakeStates(committed=started())

        stats = (await service(realm, inbound, states).poll()).ok

        assert inbound.failures == [(refused["id"], "unread")]
        assert (stats["failed"], len(inbound.applied), states.committed["user_cursor"]) == (1, 1, START + 1)

    async def test_no_keycloak_provider_row_stops_the_poll(self):
        realm, inbound, states = (
            FakeRealm(admin=[org_update(START + 1)]),
            FakeInbound(),
            FakeStates(committed=started()),
        )
        pull = service(realm, inbound, states)
        pull._providers = SimpleNamespace(get_by_name=_async(None))

        result = await pull.poll()

        assert isinstance(result.error, NotFound)
        assert (inbound.applied, states.commits) == ([], 0)

    async def test_another_worker_holding_the_lock_leaves_the_poll_to_it(self):
        realm, inbound, states = (
            FakeRealm(admin=[org_update(START + 1)]),
            FakeInbound(),
            FakeStates(committed=started(), locked=False),
        )

        stats = (await service(realm, inbound, states).poll()).ok

        assert (stats["locked_out"], realm.reads, inbound.applied, states.commits) == (1, [], [], 0)


class TestFirstPoll:
    async def test_starts_at_the_newest_events_and_applies_nothing(self):
        newest = org_update(START + 9)
        realm = FakeRealm(admin=[org_update(START + 1), newest], user=[user_event(START + 5, "LOGIN")])
        inbound, states = FakeInbound(), FakeStates()

        stats = (await service(realm, inbound, states).poll()).ok

        assert (stats["started"], inbound.applied) == (1, [])
        assert {key: states.committed[key] for key in ("admin_cursor", "user_cursor", "organizations")} == {
            "admin_cursor": START + 9,
            "user_cursor": START + 5,
            "organizations": {"org-1": "acme"},
        }
        assert newest["id"] in states.committed["admin_seen"]


# --- what each event reads as ----------------------------------------------------------------


async def read(event: dict, snapshot: RealmSnapshot | None = None, realm: FakeRealm | None = None):
    stream = EventStream.USER if "type" in event else EventStream.ADMIN
    snapshot = (
        snapshot
        if snapshot is not None
        else RealmSnapshot(**{k: v for k, v in started().items() if k in ("organizations", "client_roles")})
    )
    return await KeycloakEventReader(realm or FakeRealm()).read(stream, event, snapshot), snapshot


class TestReadings:
    async def test_an_organization_update_names_its_alias_name_domains_and_enabled(self):
        event = admin_event(
            START,
            "ORGANIZATION",
            "UPDATE",
            "organizations/org-1",
            {"id": "org-1", "alias": "acme", "name": "Acme 2", "enabled": False, "domains": [{"name": "acme.test"}]},
        )

        reading, _ = await read(event)

        assert reading.change == TenantUpdated(alias="acme", name="Acme 2", domains=("acme.test",), enabled=False)

    async def test_an_organization_delete_is_named_by_the_snapshot(self):
        reading, snapshot = await read(admin_event(START, "ORGANIZATION", "DELETE", "organizations/org-1"))

        assert (reading.change, snapshot.organizations) == (TenantDeleted(alias="acme"), {})

    async def test_a_user_update_takes_enabled_only_from_a_recorded_change_of_it(self):
        unchanged = admin_event(START, "USER", "UPDATE", "users/u1", {"firstName": "A", "enabled": True})
        disabled = admin_event(
            START, "USER", "UPDATE", "users/u1", {"enabled": False}, details={"updated_enabled": "false"}
        )

        (first, _), (second, _) = await read(unchanged), await read(disabled)

        assert (first.change.given_name, first.change.enabled) == ("A", None)
        assert second.change.enabled is False

    async def test_a_user_event_deletes_or_disables_the_account(self):
        (deleted, _), (locked, _) = (
            await read(user_event(START, "DELETE_ACCOUNT", "u1")),
            await read(user_event(START, "USER_DISABLED_BY_PERMANENT_LOCKOUT", "u1")),
        )

        assert isinstance(deleted.change, UserDeleted)
        assert (type(locked.change), locked.change.enabled) == (UserUpdated, False)

    async def test_a_member_added_is_named_by_the_username_in_the_details(self):
        realm = FakeRealm(users={"kc-1": {"id": "kc-1", "username": "bob"}})
        event = admin_event(
            START,
            "ORGANIZATION_MEMBERSHIP",
            "CREATE",
            "organizations/org-1/members",
            {"id": "org-1", "alias": "acme"},
            details={"username": "bob"},
        )

        reading, _ = await read(event, realm=realm)

        assert reading.change == MembershipChanged(alias="acme", account=reading.change.account, added=True)
        assert (reading.change.account.id, reading.change.account.username) == ("kc-1", "bob")

    async def test_a_client_role_rename_names_what_it_was(self):
        event = admin_event(
            START, "CLIENT_ROLE", "UPDATE", "roles-by-id/role-1", {"name": "documents:list", "description": "d"}
        )

        reading, snapshot = await read(event)

        assert isinstance(reading.change, ClientRoleSaved)
        assert (reading.change.previous.name, reading.change.name, reading.change.description) == (
            "documents:read",
            "documents:list",
            "d",
        )
        assert snapshot.client_roles["role-1"]["name"] == "documents:list"

    async def test_composites_name_the_api_client_roles_they_hold(self):
        snapshot = RealmSnapshot(
            client_roles={
                "role-1": {"name": "documents:read", "role_id": None, "permission_id": None},
                "role-2": {"name": "editor", "role_id": None, "permission_id": None},
            }
        )
        event = admin_event(
            START, "CLIENT_ROLE", "CREATE", "roles-by-id/role-2/composites", [{"id": "role-1"}, {"id": "elsewhere"}]
        )

        reading, _ = await read(event, snapshot)

        assert isinstance(reading.change, CompositesChanged)
        assert (reading.change.role.name, [ref.name for ref in reading.change.permissions], reading.change.added) == (
            "editor",
            ["documents:read"],
            True,
        )

    async def test_an_event_without_its_shape_is_unreadable(self):
        reading, _ = await read(admin_event(START, "ORGANIZATION", "UPDATE", "organizations/new-org"))

        assert reading.ignored is Ignored.UNREADABLE


class TestASelfServiceProfileChange:
    @pytest.mark.parametrize(
        ("verified", "email"), [(True, "new@x.test"), (False, None)], ids=["verified", "unverified"]
    )
    async def test_takes_the_email_only_once_keycloak_has_verified_it(self, verified, email):
        realm = FakeRealm(
            users={
                "u1": {"id": "u1", "username": "u", "email": "new@x.test", "emailVerified": verified, "firstName": "N"}
            }
        )

        reading, _ = await read(user_event(START, "UPDATE_PROFILE", "u1"), realm=realm)

        assert (reading.change.email, reading.change.given_name) == (email, "N")
