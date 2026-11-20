"""PermissionService — the application shell for permissions.

Each method loads state through the repository, asks ``app.domain.access`` to
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
from app.errors.identity import Conflict, IdentityError
from app.logbook import logbook
from app.repositories.base import RepositoryConflictError
from app.repositories.permission import (
    PermissionRepository,
    apply_permission,
    permission_from_row,
    permission_to_row,
)
from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.cache_invalidation import CacheInvalidationPublisher

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# The cache-invalidation operation each event is announced as.
_INVALIDATIONS: dict[type[access.AccessEvent], str] = {
    access.PermissionCreated: "create",
    access.PermissionUpdated: "update",
    access.PermissionDeleted: "delete",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _permission_payload(permission: access.Permission) -> dict:
    return {
        "id": permission.id,
        "name": permission.name.value,
        "description": permission.description,
        "created_at": permission.created_at,
        "updated_at": permission.updated_at,
    }


class PermissionService:
    """Application service for Permission operations.

    Orchestrates PermissionRepository (inner) and IdentityProviderAdapter (outer).
    The decisions are made in ``app.domain.access``.
    """

    def __init__(
        self,
        *,
        repository: PermissionRepository,
        adapter: IdentityProviderAdapter,
        publisher: CacheInvalidationPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._adapter = adapter
        self._publisher = CacheInvalidationPublisher() if publisher is None else publisher

    async def create_permission(
        self,
        *,
        name: str,
        description: str = "",
    ) -> Result[dict, IdentityError]:
        """Create a new permission: persist via repo, then sync to IdP.

        AC-2.2.2: permission CRUD via PermissionRepository + adapter.sync_permission().
        AC-2.2.5: duplicate name -> Conflict.
        """
        with tracer.start_as_current_span("PermissionService.create_permission") as span:
            span.set_attribute("permission.name", name)

            parsed = access.permission_name(name)
            if parsed.is_error():
                return Error(identity_error(parsed.error))
            command = access.CreatePermission(name=parsed.ok, description=description)

            holder = await self._repository.get_by_name(name)
            decision = access.create_permission(
                None if holder is None else permission_from_row(holder),
                command,
                permission_id=uuid.uuid4(),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            decided, created = decision.ok

            try:
                row = await self._repository.create(permission_to_row(decided))
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(identity_error(access.PermissionNameTaken(name=name)))
            permission = permission_from_row(row)
            await self._repository.commit()

            await self._invalidate(created, permission.id)

            self._log_sync_failure(
                await self._adapter.sync_permission(
                    permission_id=permission.id,
                    data={"name": permission.name.value, "description": permission.description},
                ),
                permission.id,
                "create_permission",
            )

            return Ok(_permission_payload(permission))

    async def get_permission(
        self,
        *,
        permission_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Retrieve a permission by ID."""
        with tracer.start_as_current_span("PermissionService.get_permission") as span:
            span.set_attribute("permission.id", str(permission_id))

            row = await self._repository.get(permission_id)
            if row is None:
                return Error(identity_error(access.PermissionNotFound(permission_id=permission_id)))
            return Ok(_permission_payload(permission_from_row(row)))

    async def list_permissions(self) -> Result[list[dict], IdentityError]:
        """List all permissions."""
        with tracer.start_as_current_span("PermissionService.list_permissions"):
            rows = await self._repository.list_all()
            return Ok([_permission_payload(permission_from_row(row)) for row in rows])

    async def update_permission(
        self,
        *,
        permission_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> Result[dict, IdentityError]:
        """Update permission fields, then sync to IdP."""
        with tracer.start_as_current_span("PermissionService.update_permission") as span:
            span.set_attribute("permission.id", str(permission_id))

            new_name = None
            if name is not None:
                parsed = access.permission_name(name)
                if parsed.is_error():
                    return Error(identity_error(parsed.error))
                new_name = parsed.ok

            row = await self._repository.get(permission_id)
            holder = None
            if row is not None and name is not None:
                holder = await self._repository.get_by_name(name)
            decision = access.update_permission(
                None if row is None else permission_from_row(row),
                None if holder is None else permission_from_row(holder),
                access.UpdatePermission(permission_id=permission_id, name=new_name, description=description),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            decided, updated = decision.ok
            previous_name = row.name

            apply_permission(decided, row)
            try:
                row = await self._repository.update(row)
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(Conflict(message=f"Permission name '{name}' conflicts with existing permission"))
            permission = permission_from_row(row)
            await self._repository.commit()

            await self._invalidate(updated, permission.id)

            sync_data: dict = {"name": permission.name.value, "description": permission.description}
            if previous_name != permission.name.value:
                # A provider that keys permissions by name (Keycloak client roles) renames the one it holds.
                sync_data["previous_name"] = previous_name
            self._log_sync_failure(
                await self._adapter.sync_permission(permission_id=permission.id, data=sync_data),
                permission.id,
                "update_permission",
            )

            return Ok(_permission_payload(permission))

    async def delete_permission(
        self,
        *,
        permission_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Delete a permission from DB and sync deletion to IdP."""
        with tracer.start_as_current_span("PermissionService.delete_permission") as span:
            span.set_attribute("permission.id", str(permission_id))

            row = await self._repository.get(permission_id)
            decision = access.delete_permission(
                None if row is None else permission_from_row(row),
                access.DeletePermission(permission_id=permission_id),
                now=_now(),
            )
            if decision.is_error():
                return Error(identity_error(decision.error))
            deleted = decision.ok

            if not await self._repository.delete(permission_id):
                return Error(identity_error(access.PermissionNotFound(permission_id=permission_id)))

            await self._repository.commit()

            await self._invalidate(deleted, permission_id)

            self._log_sync_failure(
                await self._adapter.delete_permission(permission_id=permission_id, data={"name": deleted.name}),
                permission_id,
                "delete_permission",
            )

            return Ok({"status": "deleted", "name": deleted.name})

    async def _invalidate(self, event: access.AccessEvent, permission_id: uuid.UUID) -> None:
        await self._publisher.publish(
            entity_type="permission",
            entity_id=permission_id,
            operation=_INVALIDATIONS[type(event)],
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
                "IdP sync failed for permission %s (%s): %s",
                entity_id,
                operation,
                result.error.message,
            )
