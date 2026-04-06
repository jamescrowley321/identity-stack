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
    ) -> None:
        self._repository = repository
        self._adapter = adapter

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
