"""Keycloak's reconciliation pass against the compose Keycloak (``keycloak-test``) and the test Postgres.

No event poller runs here: each change on the Keycloak side is made as an
administrator does in the console (the ``master`` realm's admin, through the
Admin API the console calls), each canonical change as the services make it
(rows written in the test's transaction, so the pending-push triggers fire), and
one pass of the shipped ``KeycloakReconciliation`` through ``ReconciliationService``
must bring both sides together. Every test starts with no pending push but its
own (the table is emptied in the test's transaction, which is rolled back), and
every organization, account and client role it makes is new and removed
afterwards whatever the outcome.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from app.dependencies.identity import _RECONCILIATION_LOCK_ID
from app.dependencies.keycloak_reconciliation import build_keycloak_reconciliation
from app.dependencies.keycloak_sync import build_keycloak_sync_adapter
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Permission, Role
from app.models.identity.sync_ledger import PendingPush, ReconciliationRun
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_ledger import ReconciliationRunRepository
from app.services.adapters.base import PROVIDER_SYNC, PROVIDER_SYNC_FAILED
from app.services.adapters.keycloak import PERMISSION_ATTRIBUTE, ROLE_ATTRIBUTE
from app.services.identity_provisioning import DEFAULT_ROLE_NAME
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    API_CLIENT_VARIABLE,
    ISSUER_VARIABLE,
    KeycloakAdminApi,
    KeycloakAdminUnavailable,
    connect_keycloak_admin,
)
from app.services.reconciliation import ReconciliationService
from app.services.role import RoleService
from tests.helpers.keycloak import KeycloakTokenSource
from tests.helpers.keycloak_model import ADMIN_CLIENT_ID, ADMIN_CLIENT_SECRET, API_AUDIENCE

KEYCLOAK = "keycloak"


@pytest.fixture
def environment(monkeypatch, keycloak_token_source: KeycloakTokenSource) -> KeycloakTokenSource:
    monkeypatch.setenv(ISSUER_VARIABLE, keycloak_token_source.issuer)
    monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, ADMIN_CLIENT_ID)
    monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, ADMIN_CLIENT_SECRET)
    monkeypatch.setenv(API_CLIENT_VARIABLE, API_AUDIENCE)
    return keycloak_token_source


@pytest_asyncio.fixture(loop_scope="session")
async def api(environment) -> AsyncIterator[KeycloakAdminApi]:
    async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
        yield await connect_keycloak_admin(http)


@pytest.fixture
def console(keycloak_token_source: KeycloakTokenSource) -> Iterator[httpx.Client]:
    """The realm as an administrator changes it in the console: the ``master`` realm's admin."""
    with keycloak_token_source._admin() as admin:
        yield admin


@pytest.fixture
def api_client_id(console: httpx.Client) -> str:
    return console.get("/clients", params={"clientId": API_AUDIENCE}).json()[0]["id"]


@dataclass
class Made:
    """What a test makes in the realm, removed afterwards: organization aliases, usernames, client role names."""

    aliases: list[str] = field(default_factory=list)
    usernames: list[str] = field(default_factory=list)
    client_roles: list[str] = field(default_factory=list)


@pytest.fixture
def made(console: httpx.Client, api_client_id: str) -> Iterator[Made]:
    record = Made()
    yield record
    for alias in record.aliases:
        for organization in console.get("/organizations", params={"q": f"alias:{alias}", "exact": "true"}).json():
            console.delete(f"/organizations/{organization['id']}")
    for username in record.usernames:
        for account in console.get("/users", params={"username": username, "exact": "true"}).json():
            console.delete(f"/users/{account['id']}")
    for role in console.get(f"/clients/{api_client_id}/roles").json():
        if role["name"] in record.client_roles:
            console.delete(f"/roles-by-id/{role['id']}")


@pytest_asyncio.fixture(loop_scope="session")
async def provider(db_session) -> Provider:
    found = (await db_session.execute(select(Provider).where(Provider.name == KEYCLOAK))).scalars().first()
    if found is None:
        found = Provider(name=KEYCLOAK, type=ProviderType.oidc, issuer_url="unused-by-these-tests")
        db_session.add(found)
        await db_session.flush()
    # Only this test's own changes are pending.
    await db_session.execute(delete(PendingPush))
    return found


@dataclass
class Pass:
    """One reconciliation run, as ``POST /api/internal/reconciliation/run`` makes it with ``SYNC_PROVIDER=keycloak``."""

    api: KeycloakAdminApi
    session: object

    async def run(self) -> dict:
        async def lock() -> None:
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _RECONCILIATION_LOCK_ID}
            )

        service = ReconciliationService(
            session=self.session,
            acquire_lock=lock,
            passes=[build_keycloak_reconciliation(self.api, self.session, lock)],
            run_repository=ReconciliationRunRepository(self.session),
        )
        result = await service.run()
        assert result.is_ok(), result
        return result.ok["providers"][KEYCLOAK]["stats"]


@pytest_asyncio.fixture(loop_scope="session")
async def reconcile(api, db_session, provider) -> Pass:
    return Pass(api, db_session)


# --- canonical rows, written as the services write them -----------------------------------


@dataclass
class Model:
    tenant: Tenant
    user: User
    role: Role
    alias: str
    username: str
    # Read once: a pass's rollback expires the rows above.
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID


async def _canonical(db_session, made: Made, *, roles: int = 1) -> tuple[Model, list[Role]]:
    """A tenant, a user and ``roles`` global roles assigned to them there; their names are fresh."""
    suffix = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Recon {suffix}", domains=[f"{suffix}.recon.test"], status=TenantStatus.active)
    user = User(
        email=f"recon-{suffix}@recon.test",
        user_name=f"recon-{suffix}",
        given_name="Recon",
        family_name="Before",
        status=UserStatus.active,
    )
    db_session.add_all([tenant, user])
    await db_session.flush()
    held = []
    for index in range(roles):
        role = Role(name=f"recon-{suffix}-{index}")
        db_session.add(role)
        await db_session.flush()
        db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
        made.client_roles.append(role.name)
        held.append(role)
    await db_session.flush()
    made.aliases.append(str(tenant.id))
    made.usernames.append(str(user.id))
    return Model(tenant, user, held[0], str(tenant.id), str(user.id), tenant.id, user.id, held[0].id), held


async def _pushed(db_session, reconcile: Pass, made: Made, *, roles: int = 1) -> tuple[Model, list[Role]]:
    """Canonical rows that one pass has delivered: the organization, the account, the client roles and groups."""
    model, held = await _canonical(db_session, made, roles=roles)
    await reconcile.run()
    assert await _pending(db_session, model) == {}
    for row in (model.tenant, model.user, *held):
        await db_session.refresh(row)
    assert model.tenant.external_org_id == model.alias
    return model, held


async def _pending(db_session, model: Model) -> dict[tuple[str, str], int]:
    keys = {
        ("tenant", str(model.tenant_id)),
        ("user", str(model.user_id)),
        ("membership", f"{model.user_id}:{model.tenant_id}"),
        ("role", str(model.role_id)),
    }
    rows = (await db_session.execute(select(PendingPush.kind, PendingPush.key, PendingPush.seq))).all()
    return {(kind, key): seq for kind, key, seq in rows if (kind, key) in keys}


async def _roles_of(db_session, user_id: uuid.UUID, tenant_id: uuid.UUID) -> list[str]:
    rows = await db_session.execute(
        select(Role.name)
        .join(UserTenantRole, UserTenantRole.role_id == Role.id)
        .where(UserTenantRole.user_id == user_id, UserTenantRole.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    return sorted(rows.scalars().all())


async def _default_role(db_session) -> Role:
    role = (
        (await db_session.execute(select(Role).where(Role.name == DEFAULT_ROLE_NAME, Role.tenant_id.is_(None))))
        .scalars()
        .first()
    )
    if role is None:
        role = Role(name=DEFAULT_ROLE_NAME)
        db_session.add(role)
        await db_session.flush()
    return role


# --- the realm, read as the console shows it ----------------------------------------------


def _organization(console: httpx.Client, alias: str) -> dict | None:
    found = console.get("/organizations", params={"q": f"alias:{alias}", "exact": "true"}).json()
    return found[0] if found else None


def _account(console: httpx.Client, username: str) -> dict | None:
    found = console.get("/users", params={"username": username, "exact": "true"}).json()
    return found[0] if found else None


def _member_ids(console: httpx.Client, organization: dict) -> set[str]:
    return {member["id"] for member in console.get(f"/organizations/{organization['id']}/members").json()}


def _groups_of(console: httpx.Client, organization: dict, account: dict) -> dict[str, str]:
    """The organization groups the account is in: group name -> the canonical role id it carries."""
    held = {}
    groups = console.get(f"/organizations/{organization['id']}/groups", params={"briefRepresentation": "false"})
    for group in groups.json():
        members = console.get(f"/organizations/{organization['id']}/groups/{group['id']}/members").json()
        if account["id"] in {member["id"] for member in members}:
            held[group["name"]] = (group.get("attributes") or {}).get(ROLE_ATTRIBUTE, [""])[0]
    return held


def _client_role(console: httpx.Client, api_client_id: str, name: str) -> dict | None:
    roles = console.get(f"/clients/{api_client_id}/roles", params={"briefRepresentation": "false"}).json()
    found = [role for role in roles if role["name"] == name]
    return found[0] if found else None


# --- the cases ----------------------------------------------------------------------------


class TestConvergence:
    async def test_changes_on_both_sides_converge_after_one_pass(
        self, console, api_client_id, made, db_session, provider, reconcile
    ):
        model, _ = await _pushed(db_session, reconcile, made)
        await _default_role(db_session)
        organization = _organization(console, model.alias)
        account = _account(console, model.username)

        # Keycloak side, as the console does it.
        path = f"/organizations/{organization['id']}"
        console.put(path, json={**console.get(path).json(), "name": f"Renamed {model.alias}"}).raise_for_status()
        console.put(f"/users/{account['id']}", json={"firstName": "Console", "lastName": "After"}).raise_for_status()
        permission_name = f"recon{uuid.uuid4().hex[:8]}:read"
        made.client_roles.append(permission_name)
        console.post(f"/clients/{api_client_id}/roles", json={"name": permission_name}).raise_for_status()
        # A second account, made a member in the console with no role group.
        other_suffix = uuid.uuid4().hex[:10]
        other = User(email=f"other-{other_suffix}@recon.test", user_name="other", status=UserStatus.active)
        db_session.add(other)
        await db_session.flush()
        other_id, other_email = other.id, other.email
        made.usernames.append(str(other_id))
        console.post(
            "/users", json={"username": str(other_id), "email": other_email, "enabled": True}
        ).raise_for_status()
        other_account = _account(console, str(other_id))
        console.post(f"{path}/members", json=other_account["id"]).raise_for_status()

        # Canonical side, as the services write it: a new permission and a deactivation.
        canonical_permission = Permission(name=f"recon{uuid.uuid4().hex[:8]}:write", description="Write")
        db_session.add(canonical_permission)
        made.client_roles.append(canonical_permission.name)
        other.status = UserStatus.inactive
        await db_session.flush()
        permission_id, permission_written = canonical_permission.id, canonical_permission.name

        stats = await reconcile.run()

        await db_session.refresh(model.tenant)
        await db_session.refresh(model.user)
        await db_session.refresh(other)
        assert (model.tenant.name, model.user.given_name, model.user.family_name, other.status) == (
            f"Renamed {model.alias}",
            "Console",
            "After",
            UserStatus.inactive,
        )
        pulled = (await db_session.execute(select(Permission).where(Permission.name == permission_name))).scalars()
        assert [row.name for row in pulled.all()] == [permission_name]
        assert await _roles_of(db_session, other_id, model.tenant_id) == [DEFAULT_ROLE_NAME]
        pushed = _client_role(console, api_client_id, permission_written)
        assert pushed is not None
        assert pushed["attributes"][PERMISSION_ATTRIBUTE] == [str(permission_id)]
        assert _account(console, str(other_id))["enabled"] is False
        assert await _pending(db_session, model) == {}
        assert stats["applied"] >= 4 and stats["pushed"] >= 2

        # Converged: the next pass changes nothing of these.
        again = await reconcile.run()
        await db_session.refresh(model.tenant)
        assert model.tenant.name == f"Renamed {model.alias}"
        assert await _roles_of(db_session, other_id, model.tenant_id) == [DEFAULT_ROLE_NAME]
        assert await _pending(db_session, model) == {}
        assert again["kept_pending"] == 0

        run = (
            await db_session.execute(select(ReconciliationRun).where(ReconciliationRun.provider == KEYCLOAK))
        ).scalar_one()
        assert (run.status, run.stats) == ("completed", again)


class TestFailedPushes:
    async def test_a_failed_membership_removal_is_delivered_by_the_next_pass(
        self, console, made, db_session, provider, reconcile, environment
    ):
        """identity-stack#612: the last role removed while Keycloak is down; the next pass ends the membership."""
        model, _ = await _pushed(db_session, reconcile, made)
        organization = _organization(console, model.alias)
        account = _account(console, model.username)
        assert account["id"] in _member_ids(console, organization)

        async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
            broken = await connect_keycloak_admin(http)

            async def unreachable(*_args, **_kwargs):
                raise KeycloakAdminUnavailable("admin API unreachable")

            broken.remove_member = unreachable
            service = RoleService(
                repository=RoleRepository(db_session),
                permission_repository=PermissionRepository(db_session),
                assignment_repository=UserTenantRoleRepository(db_session),
                adapter=build_keycloak_sync_adapter(broken, db_session),
            )
            result = await service.unassign_role_from_user(
                user_id=model.user_id, tenant_id=model.tenant_id, role_id=model.role_id
            )
        assert result.is_ok()
        assert result.ok[PROVIDER_SYNC] == PROVIDER_SYNC_FAILED
        assert account["id"] in _member_ids(console, organization)
        assert ("membership", f"{model.user_id}:{model.tenant_id}") in await _pending(db_session, model)

        stats = await reconcile.run()

        # Keycloak's member is not taken back in (it would hold `member` again): the removal is pushed.
        assert account["id"] not in _member_ids(console, organization)
        assert await _roles_of(db_session, model.user_id, model.tenant_id) == []
        assert await _pending(db_session, model) == {}
        assert stats["kept_pending"] >= 1

    async def test_a_failed_role_assignment_is_delivered_by_the_next_pass(
        self, console, made, db_session, provider, reconcile
    ):
        model, _ = await _pushed(db_session, reconcile, made)
        extra = Role(name=f"{model.role.name}-extra")
        db_session.add(extra)
        made.client_roles.append(extra.name)
        await db_session.flush()
        extra_id, extra_name = extra.id, extra.name
        # The role and its assignment committed, and nothing pushed: as after a push that failed.
        db_session.add(UserTenantRole(user_id=model.user_id, tenant_id=model.tenant_id, role_id=extra_id))
        await db_session.flush()
        role_name = model.role.name

        await reconcile.run()

        organization = _organization(console, model.alias)
        account = _account(console, model.username)
        assert _groups_of(console, organization, account) == {
            role_name: str(model.role_id),
            extra_name: str(extra_id),
        }
        assert await _roles_of(db_session, model.user_id, model.tenant_id) == sorted([role_name, extra_name])


class TestUndelivered:
    async def test_an_undelivered_canonical_change_is_not_overwritten(
        self, console, made, db_session, provider, reconcile
    ):
        model, _ = await _pushed(db_session, reconcile, made)
        organization = _organization(console, model.alias)
        path = f"/organizations/{organization['id']}"
        # Both sides change the tenant; the canonical change is not yet delivered (it is pending).
        console.put(path, json={**console.get(path).json(), "name": f"Console {model.alias}"}).raise_for_status()
        model.tenant.name = f"Canonical {model.alias}"
        await db_session.flush()
        assert ("tenant", str(model.tenant_id)) in await _pending(db_session, model)

        stats = await reconcile.run()

        await db_session.refresh(model.tenant)
        assert model.tenant.name == f"Canonical {model.alias}"
        assert console.get(path).json()["name"] == f"Canonical {model.alias}"
        assert stats["kept_pending"] >= 1
        assert await _pending(db_session, model) == {}

    async def test_a_delivered_change_is_overwritten_by_keycloak(self, console, made, db_session, provider, reconcile):
        """The same change once its push is confirmed: then Keycloak's later change wins."""
        model, _ = await _pushed(db_session, reconcile, made)
        organization = _organization(console, model.alias)
        path = f"/organizations/{organization['id']}"
        console.put(path, json={**console.get(path).json(), "name": f"Console {model.alias}"}).raise_for_status()

        await reconcile.run()

        await db_session.refresh(model.tenant)
        assert model.tenant.name == f"Console {model.alias}"


class TestDeletedRoleGroup:
    async def test_a_deleted_role_group_is_taken_in(self, console, made, db_session, provider, reconcile):
        """A role group deleted in the console (no member-removal event is recorded for it) unassigns its role."""
        model, held = await _pushed(db_session, reconcile, made, roles=2)
        organization = _organization(console, model.alias)
        account = _account(console, model.username)
        await db_session.refresh(held[0])
        await db_session.refresh(held[1])
        (kept_id, kept_name), dropped_name = (held[0].id, held[0].name), held[1].name
        groups = console.get(f"/organizations/{organization['id']}/groups").json()
        dropped_group = [group for group in groups if group["name"] == dropped_name][0]
        console.delete(f"/organizations/{organization['id']}/groups/{dropped_group['id']}").raise_for_status()

        await reconcile.run()

        assert await _roles_of(db_session, model.user_id, model.tenant_id) == [kept_name]
        assert _groups_of(console, organization, account) == {kept_name: str(kept_id)}

    async def test_a_member_whose_only_role_group_is_deleted_holds_the_default_role(
        self, console, made, db_session, provider, reconcile
    ):
        model, _ = await _pushed(db_session, reconcile, made)
        await _default_role(db_session)
        organization = _organization(console, model.alias)
        await db_session.refresh(model.role)
        groups = console.get(f"/organizations/{organization['id']}/groups").json()
        group = [entry for entry in groups if entry["name"] == model.role.name][0]
        console.delete(f"/organizations/{organization['id']}/groups/{group['id']}").raise_for_status()

        await reconcile.run()

        # As the pull reads a member with no role group: a canonical membership is a role in the tenant.
        assert await _roles_of(db_session, model.user_id, model.tenant_id) == [DEFAULT_ROLE_NAME]
