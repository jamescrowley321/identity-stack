"""PermissionService — domain orchestration for canonical Permission operations.

Middle layer of onion architecture: orchestrates PermissionRepository (data access)
and IdentityProviderAdapter (IdP sync). All methods return Result[T, IdentityError].
OTel spans on every method. Sync failure -> log warning, still return Ok.
"""

from __future__ import annotations

import logging
import uuid

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, IdentityError, NotFound
from app.models.identity.role import Permission
from app.repositories.permission import PermissionRepository
from app.repositories.user import RepositoryConflictError
from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.cache_invalidation import CacheInvalidationPublisher

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class PermissionService:
    """Domain service for Permission operations.

    Orchestrates PermissionRepository (inner) and IdentityProviderAdapter (outer).
    Contains NO direct SQLAlchemy imports — uses repository methods only.
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

            existing = await self._repository.get_by_name(name)
            if existing is not None:
                return Error(Conflict(message=f"Permission '{name}' already exists"))

            permission = Permission(name=name, description=description)
            try:
                permission = await self._repository.create(permission)
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(Conflict(message=f"Permission '{name}' already exists"))

            result_dict = permission.model_dump()
            permission_id = permission.id
            await self._repository.commit()

            await self._publisher.publish(entity_type="permission", entity_id=permission_id, operation="create")

            self._log_sync_failure(
                await self._adapter.sync_permission(
                    permission_id=permission_id,
                    data={"name": permission.name, "description": permission.description},
                ),
                permission_id,
                "create_permission",
            )

            return Ok(result_dict)

    async def get_permission(
        self,
        *,
        permission_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Retrieve a permission by ID."""
        with tracer.start_as_current_span("PermissionService.get_permission") as span:
            span.set_attribute("permission.id", str(permission_id))

            permission = await self._repository.get(permission_id)
            if permission is None:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))
            return Ok(permission.model_dump())

    async def list_permissions(self) -> Result[list[dict], IdentityError]:
        """List all permissions."""
        with tracer.start_as_current_span("PermissionService.list_permissions"):
            permissions = await self._repository.list_all()
            return Ok([p.model_dump() for p in permissions])

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

            permission = await self._repository.get(permission_id)
            if permission is None:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))

            if name is not None:
                existing = await self._repository.get_by_name(name)
                if existing is not None and existing.id != permission_id:
                    return Error(Conflict(message=f"Permission '{name}' already exists"))
                permission.name = name
            if description is not None:
                permission.description = description

            try:
                permission = await self._repository.update(permission)
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(Conflict(message=f"Permission name '{name}' conflicts with existing permission"))

            result_dict = permission.model_dump()
            await self._repository.commit()

            await self._publisher.publish(entity_type="permission", entity_id=permission.id, operation="update")

            self._log_sync_failure(
                await self._adapter.sync_permission(
                    permission_id=permission.id,
                    data={"name": permission.name, "description": permission.description},
                ),
                permission.id,
                "update_permission",
            )

            return Ok(result_dict)

    async def delete_permission(
        self,
        *,
        permission_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Delete a permission from DB and sync deletion to IdP."""
        with tracer.start_as_current_span("PermissionService.delete_permission") as span:
            span.set_attribute("permission.id", str(permission_id))

            permission = await self._repository.get(permission_id)
            if permission is None:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))

            perm_name = permission.name
            deleted = await self._repository.delete(permission_id)
            if not deleted:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))

            await self._repository.commit()

            await self._publisher.publish(entity_type="permission", entity_id=permission_id, operation="delete")

            self._log_sync_failure(
                await self._adapter.delete_permission(permission_id=permission_id),
                permission_id,
                "delete_permission",
            )

            return Ok({"status": "deleted", "name": perm_name})

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
