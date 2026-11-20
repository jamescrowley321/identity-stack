"""KeycloakReconciliation — Keycloak's reconciliation pass (``docs/keycloak.md#reconciliation``).

One pass runs at a time (``hold_pass``, a session lock held from start to end).
A pass has two halves.

1. **Keycloak into Postgres.** In one transaction holding the reconciliation lock
   and the event pull's lock (so no poll batch runs in between), it reads every
   mapped entity of the realm (organizations, their members and role groups with
   their members, users, the API client's roles and composites), compares them
   with the canonical model, and applies each difference as the change the event
   pull would have read (``InboundSyncService.apply_keycloak_change``), so a
   difference means what the matching event means: a member with no role group
   holds the default role ``member``, a deleted organization suspends its tenant,
   a deleted account deactivates its user. An account is a canonical user's only
   through its ``keycloak`` IdP link. Each change is applied in a savepoint.
2. **Postgres into Keycloak.** Every entity marked in ``pending_pushes`` is pushed
   through ``KeycloakSyncAdapter`` from its current canonical state, and its mark
   is removed once every push for it is delivered.

**Not yet delivered.** A canonical change is not yet delivered while its entity
has a mark in ``pending_pushes``. A database trigger writes the mark in the
transaction of the change itself (migration 009), so the mark exists as soon as
the change can be read; it stays through a push in flight or a push that failed
(``provider_sync: failed``), and only a reconciliation pass removes it, after
pushing the entity's state read after the mark, and only if no newer change
renewed it (``seq``). The first half never overwrites a marked entity: a change
whose subject is marked is rolled back. Because the marks are read before
Keycloak is, a mark a concurrent pass removes after delivering its push cannot
hide a Keycloak read older than that push. A canonical change committed during
the first half marks an entity after the marks were read: the pass reads the
marks of every entity it changed again before committing, and when one is marked
now it commits nothing and fails (``Conflict``), to be run again.

The marks are kept by the trigger, so an entity marked once stays marked until a
pass confirms it; the migration marks every entity that existed before it. An
entity that is not marked has therefore been delivered, and one Keycloak no
longer holds was deleted there.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, IdentityError, NotFound, ProviderError
from app.logbook import logbook
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.repositories.keycloak_pull import KeycloakPullStateRepository
from app.repositories.provider import ProviderRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.sync_ledger import CanonicalStateRepository, PendingPushRepository
from app.services.adapters.base import LAST_ROLE_IN_TENANT, STATUS_CHANGED, SyncError
from app.services.adapters.keycloak import (
    PROVIDER_NAME,
    SERVICE_ACCOUNT_PREFIX,
    KeycloakLinks,
    KeycloakSyncAdapter,
    Refusal,
    role_client_name,
)
from app.services.identity_provisioning import DEFAULT_ROLE_NAME
from app.services.inbound_sync import RECONCILIATION_SOURCE, InboundSyncService
from app.services.keycloak_admin import KeycloakAdminApi, KeycloakAdminError
from app.services.keycloak_events import (
    AssignmentChanged,
    ClientRoleDeleted,
    ClientRoleRef,
    ClientRoleSaved,
    CompositesChanged,
    KeycloakAccount,
    KeycloakChange,
    MembershipChanged,
    TenantDeleted,
    TenantUpdated,
    UserDeleted,
    UserUpdated,
    client_role_ref,
    role_attribute,
)
from app.services.reconciliation import PassOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# ``pending_pushes.kind`` values (migration 009), in the order their pushes run:
# an organization, a client role and an account exist before a membership names them.
TENANT, PERMISSION, ROLE, USER, MEMBERSHIP = "tenant", "permission", "role", "user", "membership"
PUSH_ORDER = (TENANT, PERMISSION, ROLE, USER, MEMBERSHIP)

# The ``subject_type`` an applied change reports -> the kind of the mark that guards it.
_MARK_KIND = {
    "tenant": TENANT,
    "user": USER,
    "permission": PERMISSION,
    "role": ROLE,
    "membership": MEMBERSHIP,
    "role_assignment": MEMBERSHIP,
}

# Refusals that mean Keycloak has nothing to change: the tenant is another
# provider's organization.
_NOTHING_TO_PUSH = frozenset({Refusal.TENANT_LINKED_ELSEWHERE})


def _mark_of(subject_type: str, subject_id: str) -> tuple[str, str] | None:
    """The mark that guards an applied change's subject: ``u:t:r`` assignments by their membership ``u:t``."""
    kind = _MARK_KIND.get(subject_type)
    if kind is None or not subject_id:
        return None
    if kind == MEMBERSHIP:
        return MEMBERSHIP, ":".join(subject_id.split(":")[:2])
    return kind, subject_id


@dataclass
class RealmState:
    """What the realm holds of every mapped entity, read in one go."""

    organizations: list[dict] = field(default_factory=list)
    members: dict[str, set[str]] = field(default_factory=dict)
    # organization id -> [(group, ids of its members)]
    groups: dict[str, list[tuple[dict, set[str]]]] = field(default_factory=dict)
    users: list[dict] = field(default_factory=list)
    client_roles: list[dict] = field(default_factory=list)
    composites: dict[str, list[dict]] = field(default_factory=dict)


class KeycloakReconciliation:
    """Keycloak's pass: Keycloak into the canonical model for what is not pending, then the pending pushes."""

    provider = PROVIDER_NAME

    def __init__(
        self,
        *,
        session: AsyncSession,
        acquire_lock: Callable[[], Coroutine[Any, Any, None]],
        hold_pass: Callable[[], AbstractAsyncContextManager[bool]],
        api: KeycloakAdminApi,
        adapter: KeycloakSyncAdapter,
        links: KeycloakLinks,
        inbound: InboundSyncService,
        state: CanonicalStateRepository,
        ledger: PendingPushRepository,
        pull_state: KeycloakPullStateRepository,
        providers: ProviderRepository,
        events: SyncEventRepository,
    ) -> None:
        self._session = session
        self._acquire_lock = acquire_lock
        self._hold_pass = hold_pass
        self._api = api
        self._adapter = adapter
        self._links = links
        self._inbound = inbound
        self._state = state
        self._ledger = ledger
        self._pull = pull_state
        self._providers = providers
        self._events = events

    async def reconcile(self) -> Result[PassOutcome, IdentityError]:
        with tracer.start_as_current_span("KeycloakReconciliation.reconcile") as span:
            run_id = str(uuid.uuid4())
            stats = {
                "applied": 0,
                "skipped": 0,
                "failed": 0,
                "kept_pending": 0,
                "pushed": 0,
                "push_failed": 0,
                "pending": 0,
            }
            # One pass at a time from start to end: the second half commits after each push,
            # which ends a transaction lock, and two passes pushing one entity from reads of
            # different ages would leave Keycloak with the older one and no mark.
            async with self._hold_pass() as held:
                if not held:
                    return Error(Conflict(message="A Keycloak reconciliation pass is already running"))
                return await self._reconcile(run_id, stats, span)

    async def _reconcile(self, run_id: str, stats: dict[str, int], span) -> Result[PassOutcome, IdentityError]:
        try:
            await self._acquire_lock()
            # The pull's lock too: a poll waits for the first half instead of interleaving with it.
            await self._pull.lock()
        except Exception as exc:
            await self._session.rollback()
            logger.error("Keycloak reconciliation: the locks could not be taken: %s", type(exc).__name__)
            return Error(ProviderError(message=f"Failed to acquire reconciliation lock: {exc}"))
        provider = await self._providers.get_by_name(PROVIDER_NAME)
        if provider is None:
            await self._session.rollback()
            return Error(NotFound(message=f"No {PROVIDER_NAME!r} provider row: run the Keycloak seed first"))
        provider_id = provider.id

        pulled = await self._pull_in(provider_id, run_id, stats)
        if pulled.is_error() and not isinstance(pulled.error, Conflict):
            span.set_attribute("reconciliation.status", "aborted")
            return Error(pulled.error)
        # After a conflict too: the pushes do not depend on the first half, and a write that
        # keeps conflicting must not keep every pending change from being delivered.
        await self._push_out(provider_id, run_id, stats)
        stats["pending"] = await self._ledger.count()
        await self._session.rollback()
        for key, value in stats.items():
            span.set_attribute(f"reconciliation.{key}", value)
        if pulled.is_error():
            span.set_attribute("reconciliation.status", "conflict")
            return Error(pulled.error)
        logger.info("Keycloak reconciliation completed: %s", stats)
        return Ok(PassOutcome(stats=stats, changes=stats["applied"] + stats["pushed"]))

    # --- Keycloak into Postgres -----------------------------------------------------

    async def _pull_in(self, provider_id: uuid.UUID, run_id: str, stats: dict[str, int]) -> Result[None, IdentityError]:
        marks = {(mark.kind, mark.key) for mark in await self._ledger.list_all()}
        tenants = await self._state.tenants_linked_to(provider_id)
        try:
            realm = await self._read_realm({tenant.external_org_id for tenant in tenants})
            changes = await self._changes(realm, tenants, provider_id)
        except KeycloakAdminError as exc:
            await self._session.rollback()
            logger.warning("Keycloak reconciliation: the realm could not be read (status %s)", exc.status)
            return Error(ProviderError(message=f"Keycloak could not be read: {exc}"))
        await self._ledger.provider_writes(True)
        touched: set[tuple[str, str]] = set()
        for change in changes:
            savepoint = await self._session.begin_nested()
            try:
                result = await self._inbound.apply_keycloak_change(
                    change, provider_id=provider_id, keycloak_event_id=run_id, source=RECONCILIATION_SOURCE
                )
            except Exception as exc:
                await savepoint.rollback()
                await self._failed(provider_id, run_id, type(change).__name__, "", type(exc).__name__, stats)
                continue
            if result.is_error():
                await savepoint.rollback()
                await self._failed(provider_id, run_id, type(change).__name__, "", result.error.message, stats)
                continue
            mark = _mark_of(result.ok["subject_type"], result.ok["subject_id"])
            if result.ok["status"] == "applied" and mark in marks:
                # A canonical change not yet delivered: Keycloak's older state does not overwrite it.
                await savepoint.rollback()
                stats["kept_pending"] += 1
                continue
            await savepoint.commit()
            if result.ok["status"] in ("applied", "skipped"):
                stats[result.ok["status"]] += 1
            if result.ok["status"] == "applied" and mark is not None:
                touched.add(mark)
        if await self._ledger.seqs(touched):
            # A canonical change committed since the marks were read: this half read the entity before it.
            await self._session.rollback()
            logger.warning("Keycloak reconciliation: a canonical change arrived during the pass; nothing applied")
            return Error(
                Conflict(message="A canonical change arrived during the Keycloak reconciliation; run it again")
            )
        await self._ledger.provider_writes(False)
        await self._session.commit()
        return Ok(None)

    async def _read_realm(self, aliases: set[str | None]) -> RealmState:
        realm = RealmState()
        realm.organizations = await self._api.organizations()
        for organization in realm.organizations:
            if organization.get("alias") not in aliases:
                continue
            organization_id = organization["id"]
            realm.members[organization_id] = {
                member["id"] for member in await self._api.organization_members(organization_id)
            }
            realm.groups[organization_id] = [
                (
                    group,
                    {
                        member["id"]
                        for member in await self._api.organization_group_members(organization_id, group["id"])
                    },
                )
                for group in await self._api.organization_groups(organization_id)
            ]
        realm.users = await self._api.users()
        realm.client_roles = await self._api.client_roles()
        for role in realm.client_roles:
            if role.get("composite") is True:
                realm.composites[role["id"]] = await self._api.role_composites(role["id"])
        return realm

    async def _changes(self, realm: RealmState, tenants: list, provider_id: uuid.UUID) -> list[KeycloakChange]:
        """Every difference between the realm and the canonical model, as the event that would have made it."""
        roles = await self._state.roles()
        permissions = await self._state.permissions()
        role_permissions = await self._state.role_permissions()
        links = await self._state.links_of(provider_id)

        refs = [(entry, client_role_ref(entry)) for entry in realm.client_roles]
        permission_refs = [(entry, ref) for entry, ref in refs if ref.is_permission]
        role_refs = [(entry, ref) for entry, ref in refs if not ref.is_permission]
        changes: list[KeycloakChange] = []

        # Tenants: each linked organization's name, domains and enabled; a linked organization gone.
        by_alias = {organization.get("alias"): organization for organization in realm.organizations}
        for tenant in tenants:
            organization = by_alias.get(tenant.external_org_id)
            if organization is None:
                # A list read a page at a time can miss an entry that moved: asked once more, by itself.
                if await self._api.find_organization(tenant.external_org_id) is None:
                    changes.append(TenantDeleted(alias=tenant.external_org_id))
                continue
            changes.append(
                TenantUpdated(
                    alias=tenant.external_org_id,
                    name=organization.get("name") if isinstance(organization.get("name"), str) else None,
                    domains=tuple(
                        entry["name"]
                        for entry in organization.get("domains") or []
                        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                    ),
                    enabled=organization.get("enabled") if isinstance(organization.get("enabled"), bool) else None,
                )
            )

        # Permissions and roles: every API client role, then the canonical ones Keycloak no longer holds.
        for entry, ref in [*permission_refs, *role_refs]:
            description = entry.get("description")
            changes.append(
                ClientRoleSaved(
                    name=ref.name,
                    description=description if isinstance(description, str) else None,
                    previous=None,
                    current=ref,
                )
            )
        held_permissions = {ref.permission_id for _, ref in permission_refs if ref.permission_id}
        unstamped_names = {ref.name for _, ref in refs if ref.permission_id is None and ref.role_id is None}
        gone_permissions = {
            permission.id
            for permission in permissions
            if str(permission.id) not in held_permissions and permission.name not in unstamped_names
        }
        for permission in permissions:
            if permission.id in gone_permissions:
                changes.append(
                    ClientRoleDeleted(ClientRoleRef(permission.name, None, str(permission.id), is_permission=True))
                )
        held_roles = {ref.role_id for _, ref in role_refs if ref.role_id}
        gone_roles: set[uuid.UUID] = set()
        for role in roles:
            client_name = role_client_name(role.name, role.tenant_id)
            if str(role.id) not in held_roles and client_name not in unstamped_names:
                gone_roles.add(role.id)
                changes.append(ClientRoleDeleted(ClientRoleRef(client_name, str(role.id), None, is_permission=False)))

        # Composites: each role client role's permissions.
        permission_by_id = {permission.id: permission for permission in permissions}
        held_by_role: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for role_id, permission_id in role_permissions:
            held_by_role[role_id].add(permission_id)
        role_by_id = {role.id: role for role in roles if role.id not in gone_roles}
        role_by_scope = {(role.name, role.tenant_id): role for role in roles if role.id not in gone_roles}
        for entry, ref in role_refs:
            composite_refs = tuple(
                ref_
                for ref_ in (client_role_ref(child) for child in realm.composites.get(entry["id"], []))
                if ref_.is_permission
            )
            if composite_refs:
                changes.append(CompositesChanged(role=ref, permissions=composite_refs, added=True))
            role = _canonical_role(ref, role_by_id, role_by_scope)
            if role is None:
                continue
            kept = {_canonical_permission(child, permissions) for child in composite_refs}
            extra = [
                ClientRoleRef(permission_by_id[permission_id].name, None, str(permission_id), is_permission=True)
                for permission_id in sorted(held_by_role[role.id] - kept, key=str)
                if permission_id in permission_by_id and permission_id not in gone_permissions
            ]
            if extra:
                changes.append(CompositesChanged(role=ref, permissions=tuple(extra), added=False))

        # Users: every account a canonical user stands for; a linked account gone.
        accounts = {account["id"]: account for account in realm.users}
        user_of = await self._account_users(realm, links)
        for account_id, user_id in user_of.items():
            account = accounts[account_id]
            first, last = account.get("firstName"), account.get("lastName")
            changes.append(
                UserUpdated(
                    _account(account),
                    # Only an email Keycloak has verified: an account's own unverified change is not taken.
                    email=account.get("email") if account.get("emailVerified") is True else None,
                    given_name=first if isinstance(first, str) else None,
                    family_name=last if isinstance(last, str) else None,
                    enabled=account.get("enabled") if isinstance(account.get("enabled"), bool) else None,
                )
            )
        for link in links:
            if link.external_sub not in accounts and await self._api.get_user(link.external_sub) is None:
                changes.append(UserDeleted(KeycloakAccount(id=link.external_sub, username=None)))

        # Memberships and role assignments, organization by organization.
        default_role = role_by_scope.get((DEFAULT_ROLE_NAME, None))
        tenant_by_alias = {tenant.external_org_id: tenant for tenant in tenants}
        assignments = await self._state.assignments_in(tenant.id for tenant in tenants)
        held: dict[tuple[uuid.UUID, uuid.UUID], set[uuid.UUID]] = defaultdict(set)
        for assignment in assignments:
            if assignment.role_id not in gone_roles:
                held[(assignment.user_id, assignment.tenant_id)].add(assignment.role_id)
        account_of = {user_id: account_id for account_id, user_id in user_of.items()}
        for organization in realm.organizations:
            tenant = tenant_by_alias.get(organization.get("alias"))
            if tenant is None or organization["id"] not in realm.members:
                continue
            alias = tenant.external_org_id
            members = realm.members[organization["id"]]
            in_groups: dict[str, set[uuid.UUID]] = defaultdict(set)
            for group, group_members in realm.groups[organization["id"]]:
                role = _group_role(group, tenant.id, role_by_id, role_by_scope)
                if role is None:
                    continue
                for member in group_members:
                    in_groups[member].add(role.id)
            for user_id, account_id in account_of.items():
                canonical = held.get((user_id, tenant.id), set())
                account = _account(accounts[account_id])
                if account_id not in members:
                    if canonical and organization["id"] not in await self._api.organizations_of(account_id):
                        changes.append(MembershipChanged(alias=alias, account=account, added=False))
                    continue
                grouped = in_groups.get(account_id, set())
                # A member in no role group holds the default role, as the pull reads a member added.
                wanted = grouped if grouped else ({default_role.id} if default_role is not None else set())
                for role_id in sorted(canonical - wanted, key=str):
                    changes.append(_assignment(alias, account, role_by_id.get(role_id), role_id, added=False))
                if not grouped:
                    changes.append(MembershipChanged(alias=alias, account=account, added=True))
                    continue
                for role_id in sorted(grouped - canonical, key=str):
                    changes.append(_assignment(alias, account, role_by_id.get(role_id), role_id, added=True))
        return changes

    async def _account_users(self, realm: RealmState, links: list) -> dict[str, uuid.UUID]:
        """The canonical user each Keycloak account stands for: the one its ``keycloak`` link names.

        A username is not taken as proof (unlike an administrator's event in the
        pull): a realm that lets a person choose a username would let them name
        another canonical user. The accounts a pass creates are linked when they
        are created (``_push``), as the adapter links a user created in a tenant.
        """
        accounts = {account["id"] for account in realm.users if not _is_service_account(account)}
        return {link.external_sub: link.user_id for link in links if link.external_sub in accounts}

    async def _failed(
        self, provider_id: uuid.UUID, run_id: str, operation: str, subject: str, message: str, stats: dict[str, int]
    ) -> None:
        logger.warning("Keycloak reconciliation: %s %s failed: %s", operation, subject, message)
        stats["failed" if not subject else "push_failed"] += 1
        await self._events.create(
            SyncEvent(
                provider_id=provider_id,
                verb=SyncEventVerb.failed,
                subject_type=subject.split(" ", 1)[0] if subject else "keycloak_reconciliation",
                subject_id=subject.split(" ", 1)[1] if subject else run_id,
                detail={"source": RECONCILIATION_SOURCE, "run": run_id, "operation": operation, "error": message[:500]},
            )
        )

    # --- Postgres into Keycloak ------------------------------------------------------

    async def _push_out(self, provider_id: uuid.UUID, run_id: str, stats: dict[str, int]) -> None:
        """Push each marked entity's current state; remove its mark once every push for it is delivered."""
        marks = await self._ledger.list_all()
        await self._session.commit()
        order = {kind: position for position, kind in enumerate(PUSH_ORDER)}
        for mark in sorted(marks, key=lambda entry: (order.get(entry.kind, len(order)), entry.key)):
            kind, key, seq, snapshot = mark.kind, mark.key, mark.seq, dict(mark.snapshot or {})
            try:
                failure = await self._push(kind, key, snapshot)
            except Exception as exc:
                await self._session.rollback()
                failure = SyncError(message=type(exc).__name__, operation=f"push_{kind}")
            if failure is None:
                await self._ledger.clear(kind, key, seq)
                stats["pushed"] += 1
            else:
                await self._failed(provider_id, run_id, failure.operation, f"{kind} {key}", failure.message, stats)
            await self._session.commit()

    async def _push(self, kind: str, key: str, snapshot: dict) -> SyncError | None:
        """Push one marked entity; ``None`` once Keycloak holds its current state."""
        if kind == MEMBERSHIP:
            user_text, _, tenant_text = key.partition(":")
            return await self._push_membership(uuid.UUID(user_text), uuid.UUID(tenant_text), snapshot)
        entity_id = uuid.UUID(key)
        if kind == TENANT:
            rows = await self._state.tenants([entity_id])
            if not rows:
                return await self._delete_tenant(entity_id, snapshot)
            tenant = rows[0]
            data = {
                "name": tenant.name,
                "self_provisioning_domains": list(tenant.domains),
                "status": tenant.status.value,
            }
            return _failure(await self._adapter.sync_tenant(tenant_id=entity_id, data=data))
        if kind == PERMISSION:
            rows = [row for row in await self._state.permissions() if row.id == entity_id]
            if not rows:
                data = {"name": snapshot["name"]} if isinstance(snapshot.get("name"), str) else {}
                return _failure(await self._adapter.delete_permission(permission_id=entity_id, data=data))
            data = {"name": rows[0].name, "description": rows[0].description}
            return _failure(await self._adapter.sync_permission(permission_id=entity_id, data=data))
        if kind == ROLE:
            rows = [row for row in await self._state.roles() if row.id == entity_id]
            if not rows:
                data = {key_: snapshot[key_] for key_ in ("name", "tenant_id") if key_ in snapshot}
                return _failure(await self._adapter.delete_role(role_id=entity_id, data=data))
            role = rows[0]
            permission_ids = {pid for rid, pid in await self._state.role_permissions() if rid == entity_id}
            names = sorted(row.name for row in await self._state.permissions() if row.id in permission_ids)
            data = {
                "name": role.name,
                "description": role.description,
                "tenant_id": None if role.tenant_id is None else str(role.tenant_id),
                "permission_names": names,
            }
            return _failure(await self._adapter.sync_role(role_id=entity_id, data=data))
        if kind == USER:
            rows = await self._state.users([entity_id])
            if not rows:
                return _failure(await self._adapter.delete_user(user_id=entity_id))
            user = rows[0]
            data = {
                "email": user.email,
                "status": user.status.value,
                "given_name": user.given_name,
                "family_name": user.family_name,
            }
            # Only an account this push creates is linked: one already under the canonical id is not proof.
            creates = not await self._links.has_link(entity_id) and await self._api.find_user(key) is None
            # The profile first (it creates a missing account), then the status, which never creates one.
            failure = _failure(await self._adapter.sync_user(user_id=entity_id, data=data))
            if failure is not None:
                return failure
            if creates:
                created = await self._api.find_user(key)
                if created is None:
                    return SyncError(message=Refusal.USER_NOT_IN_KEYCLOAK.value, operation="link_user")
                await self._links.link_user(entity_id, created["id"], user.email)
            return _failure(await self._adapter.sync_user(user_id=entity_id, data={**data, STATUS_CHANGED: True}))
        logger.warning("Keycloak reconciliation: a pending push of unknown kind %r is left as it is", kind)
        return SyncError(message=f"unknown pending push kind {kind!r}", operation="push")

    async def _delete_tenant(self, tenant_id: uuid.UUID, snapshot: dict) -> SyncError | None:
        """The tenant row is gone, and its link with it: its organization is the one the snapshot names."""
        provider = await self._providers.get_by_name(PROVIDER_NAME)
        alias = snapshot.get("external_org_id")
        if (
            provider is None
            or not isinstance(alias, str)
            or snapshot.get("external_org_provider_id") != str(provider.id)
        ):
            if isinstance(alias, str):
                # Linked to another provider's organization: nothing of it is Keycloak's.
                return None
            return _failure(await self._adapter.delete_tenant(tenant_id=tenant_id))
        try:
            organization = await self._api.find_organization(alias)
            if organization is not None:
                await self._api.delete_organization(organization["id"])
        except KeycloakAdminError as exc:
            return SyncError(message=str(exc), operation="delete_tenant", context={"status": exc.status})
        return None

    async def _push_membership(self, user_id: uuid.UUID, tenant_id: uuid.UUID, snapshot: dict) -> SyncError | None:
        """Make the user's role groups in the tenant's organization exactly their canonical roles there."""
        if not await self._state.tenants([tenant_id]):
            # The tenant is gone; its organization is the tenant push's.
            return None
        if await self._links.linked_elsewhere(tenant_id):
            # The tenant is another provider's organization: Keycloak holds no membership of it.
            return None
        role_ids = {assignment.role_id for assignment in await self._state.assignments_of(user_id, tenant_id)}
        if not role_ids:
            removed_role = _uuid(snapshot.get("role_id")) or uuid.UUID(int=0)
            result = await self._adapter.delete_role_assignment(
                user_id=user_id, tenant_id=tenant_id, role_id=removed_role, data={LAST_ROLE_IN_TENANT: True}
            )
            if result.is_error() and _refusal(result.error) is Refusal.USER_NOT_IN_KEYCLOAK:
                # No account: no membership to end.
                return None
            return _failure(result)
        for role_id in sorted(role_ids, key=str):
            failure = _failure(
                await self._adapter.sync_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id)
            )
            if failure is not None:
                return failure
        for role_id in await self._groups_beyond(user_id, tenant_id, role_ids):
            failure = _failure(
                await self._adapter.delete_role_assignment(user_id=user_id, tenant_id=tenant_id, role_id=role_id)
            )
            if failure is not None:
                return failure
        return None

    async def _groups_beyond(
        self, user_id: uuid.UUID, tenant_id: uuid.UUID, role_ids: set[uuid.UUID]
    ) -> list[uuid.UUID]:
        """The roles of the organization groups the user's account is in that are not among ``role_ids``."""
        tenant = (await self._state.tenants([tenant_id]))[0]
        provider = await self._providers.get_by_name(PROVIDER_NAME)
        if provider is None:
            return []
        linked = tenant.external_org_provider_id == provider.id
        organization = await self._api.find_organization(tenant.external_org_id if linked else str(tenant_id))
        if organization is None:
            return []
        accounts = {link.external_sub for link in await self._state.links_of(provider.id) if link.user_id == user_id}
        named = await self._api.find_user(str(user_id))
        if named is not None:
            accounts.add(named["id"])
        roles = await self._state.roles()
        role_by_id = {role.id: role for role in roles}
        role_by_scope = {(role.name, role.tenant_id): role for role in roles}
        beyond: list[uuid.UUID] = []
        for group in await self._api.organization_groups(organization["id"]):
            members = {
                member["id"] for member in await self._api.organization_group_members(organization["id"], group["id"])
            }
            if not members & accounts:
                continue
            role = _group_role(group, tenant_id, role_by_id, role_by_scope)
            role_id = role.id if role is not None else _uuid(role_attribute(group))
            if role_id is None:
                logger.warning(
                    "Keycloak reconciliation: group %s stands for no canonical role; left as it is", group["id"]
                )
                continue
            if role_id not in role_ids:
                beyond.append(role_id)
        return beyond


def _is_service_account(account: dict) -> bool:
    """A client's service account: no person signs in with it, so no canonical user stands for it."""
    username = account.get("username")
    return bool(account.get("serviceAccountClientId")) or (
        isinstance(username, str) and username.startswith(SERVICE_ACCOUNT_PREFIX)
    )


def _account(account: dict) -> KeycloakAccount:
    """A linked account: found by its link, never by its username (``admin_named`` off)."""
    username = account.get("username")
    return KeycloakAccount(
        id=account["id"], username=username if isinstance(username, str) else None, admin_named=False
    )


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


def _canonical_role(ref: ClientRoleRef, role_by_id: dict, role_by_scope: dict):
    if ref.role_id is not None:
        return role_by_id.get(uuid.UUID(ref.role_id))
    name, _, suffix = ref.name.rpartition("@")
    tenant_id = _uuid(suffix) if name else None
    return role_by_scope.get((name, tenant_id)) if tenant_id is not None else role_by_scope.get((ref.name, None))


def _canonical_permission(ref: ClientRoleRef, permissions: list) -> uuid.UUID | None:
    if ref.permission_id is not None:
        return uuid.UUID(ref.permission_id)
    return next((permission.id for permission in permissions if permission.name == ref.name), None)


def _group_role(group: dict, tenant_id: uuid.UUID, role_by_id: dict, role_by_scope: dict):
    """The canonical role an organization group stands for: its ``identity-stack.role-id``, else its name."""
    role_id = _uuid(role_attribute(group))
    if role_id is not None:
        role = role_by_id.get(role_id)
    else:
        name = group.get("name")
        role = (
            _canonical_role(ClientRoleRef(name, None, None, False), role_by_id, role_by_scope)
            if isinstance(name, str)
            else None
        )
    if role is None or role.tenant_id not in (None, tenant_id):
        return None
    return role


def _assignment(alias: str, account: KeycloakAccount, role, role_id: uuid.UUID, *, added: bool) -> AssignmentChanged:
    group_name = role_client_name(role.name, role.tenant_id) if role is not None else str(role_id)
    return AssignmentChanged(alias=alias, account=account, group_name=group_name, role_id=str(role_id), added=added)


def _refusal(error: SyncError) -> Refusal | None:
    refusal = (error.context or {}).get("refusal")
    return refusal if isinstance(refusal, Refusal) else None


def _failure(result: Result[None, SyncError]) -> SyncError | None:
    """``None`` for a delivered push, or for a refusal that leaves Keycloak nothing to change."""
    if result.is_ok():
        return None
    if _refusal(result.error) in _NOTHING_TO_PUSH:
        return None
    return result.error
