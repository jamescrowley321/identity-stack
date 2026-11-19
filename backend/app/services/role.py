"""RoleService — the application shell for roles and role assignments.

Each method loads state through the repositories, asks ``app.domain.access`` to
decide, persists what was decided, then invalidates caches and syncs to the IdP.
All methods return Result[T, IdentityError]. OTel spans on every method.
Sync failure -> log warning, still return Ok.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from expression import Error, Ok, Result
from opentelemetry import trace

from app.domain import access
from app.errors.access import identity_error
from app.errors.identity import Conflict, IdentityError, ValidationError
from app.logbook import logbook
from app.repositories.assignment import UserTenantRoleRepository, assignment_from_row, assignment_to_row
from app.repositories.base import RepositoryConflictError
from app.repositories.permission import PermissionRepository, permission_from_row
from app.repositories.role import RoleRepository, apply_role, role_from_row, role_to_row
from app.services.adapters.base import (
    LAST_ROLE_IN_TENANT,
    PROVIDER_SYNC,
    PROVIDER_SYNC_FAILED,
    UNANSWERED,
    IdentityProviderAdapter,
    SyncError,
)
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.memberships import restore_memberships

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# The cache-invalidation operation each event is announced as.
_INVALIDATIONS: dict[type[access.AccessEvent], str] = {
    access.RoleCreated: "create",
    access.RoleUpdated: "update",
    access.PermissionMappedToRole: "update",
    access.RoleDeleted: "delete",
    access.RoleAssigned: "assign",
    access.RoleUnassigned: "unassign",
}


NO_ROLES_TO_ENROL = "A member is given at least one role"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _role_payload(role: access.Role) -> dict:
    return {
        "id": role.id,
        "name": role.name.value,
        "description": role.description,
        "tenant_id": role.tenant_id,
        "created_at": role.created_at,
        "updated_at": role.updated_at,
    }


def _scope(role: access.Role) -> dict:
    """The sync data that says whose role it is: ``tenant_id`` a tenant's own, ``None`` a global one."""
    return {"tenant_id": None if role.tenant_id is None else str(role.tenant_id)}


class RoleService:
    """Application service for Role operations.

    Orchestrates RoleRepository, PermissionRepository, UserTenantRoleRepository (inner)
    and IdentityProviderAdapter (outer). The decisions are made in ``app.domain.access``.
    """

    def __init__(
        self,
        *,
        repository: RoleRepository,
        permission_repository: PermissionRepository,
        assignment_repository: UserTenantRoleRepository,
        adapter: IdentityProviderAdapter,
        publisher: CacheInvalidationPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._permission_repository = permission_repository
        self._assignment_repository = assignment_repository
        self._adapter = adapter
        self._publisher = CacheInvalidationPublisher() if publisher is None else publisher

    async def create_role(
        self,
        *,
        name: str,
        description: str = "",
        tenant_id: uuid.UUID | None = None,
        permission_names: list[str] | None = None,
    ) -> Result[dict, IdentityError]:
        """Create a new role: persist via repo, then sync to IdP.

        AC-2.2.1: role CRUD via RoleRepository + adapter.sync_role().
        AC-2.2.5: duplicate name within scope -> Conflict.
        """
        with tracer.start_as_current_span("RoleService.create_role") as span:
            span.set_attribute("role.name", name)
            if tenant_id is not None:
                span.set_attribute("tenant.id", str(tenant_id))

            parsed = access.role_name(name)
            if parsed.is_error():
                return Error(identity_error(parsed.error))
            command = access.CreateRole(name=parsed.ok, description=description, tenant_id=tenant_id)

            holder = await self._repository.get_by_name(name, tenant_id)
            decision = access.create_role(
                None if holder is None else role_from_row(holder),
                command,
                role_id=uuid.uuid4(),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            decided, created = decision.ok

            try:
                row = await self._repository.create(role_to_row(decided))
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(identity_error(access.RoleNameTaken(name=name)))
            role = role_from_row(row)

            names = tuple(permission_names or ())
            known = []
            for permission_name in names:
                permission_row = await self._permission_repository.get_by_name(permission_name)
                if permission_row is not None:
                    known.append(permission_from_row(permission_row))
            mapping = access.map_named_permissions(
                role,
                known,
                access.MapNamedPermissions(role_id=role.id, names=names),
                now=created.occurred_at,
            )
            if mapping.is_error():
                await self._repository.rollback()
                return Error(identity_error(mapping.error))
            for mapped in mapping.ok:
                try:
                    await self._repository.add_permission(mapped.role_id, mapped.permission_id)
                except RepositoryConflictError:
                    pass  # already mapped, skip

            await self._repository.commit()

            await self._invalidate(created, role.id, created.tenant_id)

            sync_data: dict = {"name": role.name.value, "description": role.description, **_scope(role)}
            if names:
                sync_data["permission_names"] = list(names)
            self._log_sync_failure(
                await self._adapter.sync_role(role_id=role.id, data=sync_data),
                role.id,
                "create_role",
            )

            return Ok(_role_payload(role))

    async def get_role(
        self,
        *,
        role_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Retrieve a role by ID."""
        with tracer.start_as_current_span("RoleService.get_role") as span:
            span.set_attribute("role.id", str(role_id))

            row = await self._repository.get(role_id)
            if row is None:
                return Error(identity_error(access.RoleNotFound(role_id=role_id)))
            return Ok(_role_payload(role_from_row(row)))

    async def map_permission_to_role(
        self,
        *,
        role_id: uuid.UUID,
        permission_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Map a permission to a role, then sync to IdP.

        AC-2.2.2: permission mapping via RoleRepository.add_permission().
        """
        with tracer.start_as_current_span("RoleService.map_permission_to_role") as span:
            span.set_attribute("role.id", str(role_id))
            span.set_attribute("permission.id", str(permission_id))

            row = await self._repository.get(role_id)
            role = None if row is None else role_from_row(row)
            permission_row = None if role is None else await self._permission_repository.get(permission_id)
            decision = access.map_permission(
                role,
                None if permission_row is None else permission_from_row(permission_row),
                access.MapPermission(role_id=role_id, permission_id=permission_id),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            mapped = decision.ok

            try:
                await self._repository.add_permission(role_id, permission_id)
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(
                    identity_error(access.PermissionAlreadyMapped(role_id=role_id, permission_id=permission_id))
                )

            permission_rows = await self._repository.get_permissions(role_id)
            permission_names = [permission_from_row(p).name.value for p in permission_rows]
            await self._repository.commit()

            await self._invalidate(mapped, role_id, role.tenant_id)

            self._log_sync_failure(
                await self._adapter.sync_role(
                    role_id=role_id,
                    data={
                        "name": role.name.value,
                        "description": role.description,
                        **_scope(role),
                        "permission_names": permission_names,
                    },
                ),
                role_id,
                "map_permission_to_role",
            )

            return Ok({"role_id": str(mapped.role_id), "permission_id": str(mapped.permission_id)})

    async def assign_role_to_user(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        assigned_by: uuid.UUID | None = None,
    ) -> Result[dict, IdentityError]:
        """Assign a role to a user within a tenant, then sync to IdP.

        AC-2.2.4: role assignment via UserTenantRoleRepository + adapter.sync_role_assignment().
        """
        with tracer.start_as_current_span("RoleService.assign_role_to_user") as span:
            span.set_attribute("user.id", str(user_id))
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("role.id", str(role_id))

            # A role is given only to a user who already holds one in the tenant, whatever
            # the adapter: otherwise a tenant admin could pull a user of another tenant
            # (an operator among them) into theirs (#693). The first membership comes
            # from sign-in provisioning, as #592 has it for every provider.
            if not await self._assignment_repository.list_by_user_tenant(user_id, tenant_id):
                return Error(identity_error(access.NotATenantMember(user_id=user_id, tenant_id=tenant_id)))

            row = await self._repository.get(role_id)
            held_row = None if row is None else await self._assignment_repository.get(user_id, tenant_id, role_id)
            decision = access.assign_role(
                None if row is None else role_from_row(row),
                None if held_row is None else assignment_from_row(held_row),
                access.AssignRole(user_id=user_id, tenant_id=tenant_id, role_id=role_id, assigned_by=assigned_by),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            decided, assigned = decision.ok

            try:
                assignment_row = await self._assignment_repository.create(assignment_to_row(decided))
            except RepositoryConflictError:
                await self._assignment_repository.rollback()
                return Error(
                    identity_error(access.RoleAlreadyAssigned(user_id=user_id, tenant_id=tenant_id, role_id=role_id))
                )
            assignment = assignment_from_row(assignment_row)
            await self._assignment_repository.commit()

            await self._invalidate(assigned, role_id, tenant_id)

            self._log_sync_failure(
                await self._adapter.sync_role_assignment(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    role_id=role_id,
                    data={"role_name": assigned.role_name},
                ),
                role_id,
                "assign_role_to_user",
            )

            return Ok(
                {
                    "user_id": str(assignment.user_id),
                    "tenant_id": str(assignment.tenant_id),
                    "role_id": str(assignment.role_id),
                    "assigned_by": str(assignment.assigned_by) if assignment.assigned_by else None,
                    "assigned_at": assignment.assigned_at.isoformat() if assignment.assigned_at else None,
                }
            )

    async def assignable_roles(
        self, *, tenant_id: uuid.UUID, role_names: list[str]
    ) -> Result[list[uuid.UUID], IdentityError]:
        """The id of each named role assignable in the tenant: the tenant's own of that name, else the global one.

        ``ValidationError`` for a name the tenant has no role of; nothing is written.
        """
        with tracer.start_as_current_span("RoleService.assignable_roles") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            if not role_names:
                return Error(ValidationError(message=NO_ROLES_TO_ENROL))
            role_ids: list[uuid.UUID] = []
            for name in dict.fromkeys(role_names):
                row = await self._repository.get_by_name(name, tenant_id) or await self._repository.get_by_name(name)
                if row is None:
                    return Error(ValidationError(message=f"Role '{name}' not found"))
                role_ids.append(row.id)
            return Ok(role_ids)

    async def stage_member_roles(
        self, *, user_id: uuid.UUID, tenant_id: uuid.UUID, role_ids: list[uuid.UUID]
    ) -> Result[list[access.RoleAssigned], IdentityError]:
        """Write the new member's assignments in the caller's transaction, without committing or pushing.

        For an invitation (``UserService.create_member``), whose user and roles
        commit together or not at all: on an ``Error`` the caller rolls back. Each
        returned event is pushed after the commit by ``push_member_roles``.
        """
        with tracer.start_as_current_span("RoleService.stage_member_roles") as span:
            span.set_attribute("user.id", str(user_id))
            span.set_attribute("tenant.id", str(tenant_id))
            if not role_ids:
                return Error(ValidationError(message=NO_ROLES_TO_ENROL))
            staged: list[access.RoleAssigned] = []
            for role_id in role_ids:
                row = await self._repository.get(role_id)
                held = None if row is None else await self._assignment_repository.get(user_id, tenant_id, role_id)
                decision = access.assign_role(
                    None if row is None else role_from_row(row),
                    None if held is None else assignment_from_row(held),
                    access.AssignRole(user_id=user_id, tenant_id=tenant_id, role_id=role_id, assigned_by=None),
                    now=_now(),
                )
                if decision.is_error():
                    return Error(identity_error(decision.error))
                decided, assigned = decision.ok
                try:
                    await self._assignment_repository.create(assignment_to_row(decided))
                except RepositoryConflictError:
                    return Error(
                        identity_error(
                            access.RoleAlreadyAssigned(user_id=user_id, tenant_id=tenant_id, role_id=role_id)
                        )
                    )
                staged.append(assigned)
            return Ok(staged)

    async def push_member_roles(self, assigned: list[access.RoleAssigned]) -> Result[bool, IdentityError]:
        """After the commit: invalidate and push each staged assignment (Keycloak: organization member and group).

        ``Ok(True)`` when every push succeeded; a failed one is logged and makes it ``Ok(False)``.
        """
        with tracer.start_as_current_span("RoleService.push_member_roles") as span:
            span.set_attribute("assignments.count", len(assigned))
            every_pushed = True
            for event in assigned:
                await self._invalidate(event, event.role_id, event.tenant_id)
                pushed = await self._adapter.sync_role_assignment(
                    user_id=event.user_id,
                    tenant_id=event.tenant_id,
                    role_id=event.role_id,
                    data={"role_name": event.role_name},
                )
                self._log_sync_failure(pushed, event.role_id, "push_member_roles")
                every_pushed = every_pushed and pushed.is_ok()
            return Ok(every_pushed)

    async def list_roles(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
    ) -> Result[list[dict], IdentityError]:
        """List roles filtered by tenant scope."""
        with tracer.start_as_current_span("RoleService.list_roles") as span:
            if tenant_id is not None:
                span.set_attribute("tenant.id", str(tenant_id))

            rows = await self._repository.list_by_tenant(tenant_id)
            return Ok([_role_payload(role_from_row(row)) for row in rows])

    async def update_role(
        self,
        *,
        role_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
        permission_names: list[str] | None = None,
    ) -> Result[dict, IdentityError]:
        """Update role fields, then sync to IdP."""
        with tracer.start_as_current_span("RoleService.update_role") as span:
            span.set_attribute("role.id", str(role_id))

            new_name = None
            if name is not None:
                parsed = access.role_name(name)
                if parsed.is_error():
                    return Error(identity_error(parsed.error))
                new_name = parsed.ok

            row = await self._repository.get(role_id)
            current = None if row is None else role_from_row(row)
            holder = None
            if current is not None and name is not None:
                holder = await self._repository.get_by_name(name, current.tenant_id)
            decision = access.update_role(
                current,
                None if holder is None else role_from_row(holder),
                access.UpdateRole(role_id=role_id, name=new_name, description=description),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            decided, updated = decision.ok
            previous_name = current.name.value

            apply_role(decided, row)
            try:
                row = await self._repository.update(row)
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(Conflict(message=f"Role name '{name}' conflicts with existing role"))
            role = role_from_row(row)
            await self._repository.commit()

            await self._invalidate(updated, role.id, role.tenant_id)

            sync_data: dict = {"name": role.name.value, "description": role.description, **_scope(role)}
            if previous_name != role.name.value:
                # A provider that keys roles by name (Keycloak client roles) renames the one it holds.
                sync_data["previous_name"] = previous_name
            if permission_names is not None:
                sync_data["permission_names"] = permission_names
            self._log_sync_failure(
                await self._adapter.sync_role(role_id=role.id, data=sync_data),
                role.id,
                "update_role",
            )

            return Ok(_role_payload(role))

    async def delete_role(
        self,
        *,
        role_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Delete a role from DB and sync deletion to IdP."""
        with tracer.start_as_current_span("RoleService.delete_role") as span:
            span.set_attribute("role.id", str(role_id))

            row = await self._repository.get(role_id)
            decision = access.delete_role(
                None if row is None else role_from_row(row),
                access.DeleteRole(role_id=role_id),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            deleted = decision.ok

            # The delete cascades to the role's assignments: who held it is read
            # first, under a lock on the role's row so no assignment of it can
            # commit between the read and the delete and be dropped unseen.
            holders: list[tuple[uuid.UUID, uuid.UUID]] = []
            if self._adapter.ends_memberships is True:
                await self._repository.lock(role_id)
                assignments = await self._assignment_repository.list_by_role(role_id)
                holders = sorted({(row.user_id, row.tenant_id) for row in assignments})

            if not await self._repository.delete(role_id):
                return Error(identity_error(access.RoleNotFound(role_id=role_id)))

            await self._repository.commit()

            await self._invalidate(deleted, role_id, deleted.tenant_id)

            self._log_sync_failure(
                await self._adapter.delete_role(
                    role_id=role_id,
                    data={
                        "name": deleted.name,
                        "tenant_id": None if deleted.tenant_id is None else str(deleted.tenant_id),
                    },
                ),
                role_id,
                "delete_role",
            )

            payload: dict = {"status": "deleted", "name": deleted.name}
            if holders and not await self._end_memberships(holders, role_id, deleted.name):
                payload[PROVIDER_SYNC] = PROVIDER_SYNC_FAILED
            return Ok(payload)

    async def unassign_role_from_user(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Remove a role assignment from a user within a tenant, then sync to IdP."""
        with tracer.start_as_current_span("RoleService.unassign_role_from_user") as span:
            span.set_attribute("user.id", str(user_id))
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("role.id", str(role_id))

            row = await self._repository.get(role_id)
            role_name = role_from_row(row).name.value if row else str(role_id)

            # The delete's row count is the state: whether the user held the role.
            was_held = await self._assignment_repository.delete(user_id, tenant_id, role_id)
            decision = access.unassign_role(
                was_held,
                access.UnassignRole(user_id=user_id, tenant_id=tenant_id, role_id=role_id),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            unassigned = decision.ok

            await self._assignment_repository.commit()

            # The unassignment is committed: its cached permissions go first, so a
            # failure below cannot leave the revoked role live.
            await self._invalidate(unassigned, role_id, tenant_id)

            # No role left in the tenant: the user no longer belongs to it, and an
            # adapter that keeps a membership (Keycloak organizations) ends it.
            # Counted after the commit: counted before it, two concurrent unassigns
            # of a user's last two roles would each still see the other's row.
            sync_data: dict = {"role_name": role_name}
            if not await self._assignment_repository.list_by_user_tenant(user_id, tenant_id):
                sync_data[LAST_ROLE_IN_TENANT] = True

            pushed = await self._adapter.delete_role_assignment(
                user_id=user_id,
                tenant_id=tenant_id,
                role_id=role_id,
                data=sync_data,
            )
            payload: dict = {
                "status": "removed",
                "user_id": str(unassigned.user_id),
                "tenant_id": str(unassigned.tenant_id),
                "role_id": str(unassigned.role_id),
            }
            if self._adapter.ends_memberships is not True or LAST_ROLE_IN_TENANT not in sync_data:
                self._log_sync_failure(pushed, role_id, "unassign_role_from_user")
            elif pushed.is_error():
                self._log_revocation_failure(pushed, user_id, tenant_id)
                payload[PROVIDER_SYNC] = PROVIDER_SYNC_FAILED
            else:
                await restore_memberships(self._assignment_repository, self._adapter, [(user_id, tenant_id)])
            return Ok(payload)

    async def _end_memberships(
        self,
        pairs: list[tuple[uuid.UUID, uuid.UUID]],
        role_id: uuid.UUID,
        role_name: str,
    ) -> bool:
        """End the membership of each (user, tenant) pair left with no role; whether every push succeeded.

        Who still holds a role is read once for all the pairs, after the commit.
        The pushes stop at the first the provider gave no answer to: each would
        wait out the same timeout, so the rest are logged as not pushed instead.
        """
        still = {(row.user_id, row.tenant_id) for row in await self._assignment_repository.list_by_user_tenants(pairs)}
        leaving = [pair for pair in pairs if pair not in still]
        ended: list[tuple[uuid.UUID, uuid.UUID]] = []
        succeeded = True
        for index, (user_id, tenant_id) in enumerate(leaving):
            pushed = await self._adapter.delete_role_assignment(
                user_id=user_id,
                tenant_id=tenant_id,
                role_id=role_id,
                data={"role_name": role_name, LAST_ROLE_IN_TENANT: True},
            )
            if pushed.is_ok():
                ended.append((user_id, tenant_id))
                continue
            succeeded = False
            self._log_revocation_failure(pushed, user_id, tenant_id)
            if (pushed.error.context or {}).get(UNANSWERED) is True:
                for skipped_user, skipped_tenant in leaving[index + 1 :]:
                    logger.error(
                        "IdP revocation not pushed: membership of user %s in tenant %s left after no answer",
                        skipped_user,
                        skipped_tenant,
                    )
                break
        await restore_memberships(self._assignment_repository, self._adapter, ended)
        return succeeded

    @staticmethod
    def _log_revocation_failure(result: Result[None, SyncError], user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        """A membership the provider still holds after the canonical one ended: an error, not a warning."""
        if result.is_error():
            logger.error(
                "IdP revocation failed: membership of user %s in tenant %s not ended (%s): %s",
                user_id,
                tenant_id,
                result.error.operation,
                result.error.message,
            )

    async def _invalidate(
        self,
        event: access.AccessEvent,
        role_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
    ) -> None:
        await self._publisher.publish(
            entity_type="role",
            entity_id=role_id,
            operation=_INVALIDATIONS[type(event)],
            tenant_id=tenant_id,
        )

    @staticmethod
    def _log_sync_failure(
        result: Result[None, SyncError],
        entity_id: uuid.UUID,
        operation: str,
    ) -> None:
        """Log adapter sync failures as warnings without propagating."""
        if result.is_error():
            logger.warning(
                "IdP sync failed for role %s (%s): %s",
                entity_id,
                operation,
                result.error.message,
            )
