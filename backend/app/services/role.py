"""RoleService — domain orchestration for canonical Role operations.

Middle layer of onion architecture: orchestrates RoleRepository, PermissionRepository,
UserTenantRoleRepository (data access) and IdentityProviderAdapter (IdP sync).
All methods return Result[T, IdentityError]. OTel spans on every method.
Sync failure -> log warning, still return Ok.
"""

from __future__ import annotations

import logging
import uuid

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, IdentityError, NotFound
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.user import RepositoryConflictError
from app.services.adapters.base import IdentityProviderAdapter, SyncError

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class RoleService:
    """Domain service for Role operations.

    Orchestrates RoleRepository, PermissionRepository, UserTenantRoleRepository (inner)
    and IdentityProviderAdapter (outer).
    Contains NO direct SQLAlchemy imports — uses repository methods only.
    """

    def __init__(
        self,
        *,
        repository: RoleRepository,
        permission_repository: PermissionRepository,
        assignment_repository: UserTenantRoleRepository,
        adapter: IdentityProviderAdapter,
    ) -> None:
        self._repository = repository
        self._permission_repository = permission_repository
        self._assignment_repository = assignment_repository
        self._adapter = adapter

    async def create_role(
        self,
        *,
        name: str,
        description: str = "",
        tenant_id: uuid.UUID | None = None,
    ) -> Result[dict, IdentityError]:
        """Create a new role: persist via repo, then sync to IdP.

        AC-2.2.1: role CRUD via RoleRepository + adapter.sync_role().
        AC-2.2.5: duplicate name within scope -> Conflict.
        """
        with tracer.start_as_current_span("RoleService.create_role") as span:
            span.set_attribute("role.name", name)
            if tenant_id is not None:
                span.set_attribute("tenant.id", str(tenant_id))

            existing = await self._repository.get_by_name(name, tenant_id)
            if existing is not None:
                return Error(Conflict(message=f"Role '{name}' already exists in this scope"))

            role = Role(name=name, description=description, tenant_id=tenant_id)
            try:
                role = await self._repository.create(role)
            except RepositoryConflictError:
                return Error(Conflict(message=f"Role '{name}' already exists in this scope"))

            result_dict = role.model_dump()
            role_id = role.id
            await self._repository.commit()

            self._log_sync_failure(
                await self._adapter.sync_role(
                    role_id=role_id,
                    data={"name": role.name, "description": role.description},
                ),
                role_id,
                "create_role",
            )

            return Ok(result_dict)

    async def get_role(
        self,
        *,
        role_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Retrieve a role by ID."""
        with tracer.start_as_current_span("RoleService.get_role") as span:
            span.set_attribute("role.id", str(role_id))

            role = await self._repository.get(role_id)
            if role is None:
                return Error(NotFound(message=f"Role '{role_id}' not found"))
            return Ok(role.model_dump())

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

            role = await self._repository.get(role_id)
            if role is None:
                return Error(NotFound(message=f"Role '{role_id}' not found"))

            permission = await self._permission_repository.get(permission_id)
            if permission is None:
                return Error(NotFound(message=f"Permission '{permission_id}' not found"))

            try:
                mapping = await self._repository.add_permission(role_id, permission_id)
            except RepositoryConflictError:
                return Error(Conflict(message=f"Permission '{permission_id}' is already mapped to role '{role_id}'"))

            result_dict = {
                "role_id": str(mapping.role_id),
                "permission_id": str(mapping.permission_id),
            }
            await self._repository.commit()

            permissions = await self._repository.get_permissions(role_id)
            permission_names = [p.name for p in permissions]
            self._log_sync_failure(
                await self._adapter.sync_role(
                    role_id=role_id,
                    data={
                        "name": role.name,
                        "description": role.description,
                        "permission_names": permission_names,
                    },
                ),
                role_id,
                "map_permission_to_role",
            )

            return Ok(result_dict)

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

            role = await self._repository.get(role_id)
            if role is None:
                return Error(NotFound(message=f"Role '{role_id}' not found"))

            existing = await self._assignment_repository.get(user_id, tenant_id, role_id)
            if existing is not None:
                return Error(Conflict(message=f"User '{user_id}' already has role '{role_id}' in tenant '{tenant_id}'"))

            assignment = UserTenantRole(
                user_id=user_id,
                tenant_id=tenant_id,
                role_id=role_id,
                assigned_by=assigned_by,
            )
            try:
                assignment = await self._assignment_repository.create(assignment)
            except RepositoryConflictError:
                return Error(Conflict(message=f"User '{user_id}' already has role '{role_id}' in tenant '{tenant_id}'"))

            result_dict = {
                "user_id": str(assignment.user_id),
                "tenant_id": str(assignment.tenant_id),
                "role_id": str(assignment.role_id),
                "assigned_by": str(assignment.assigned_by) if assignment.assigned_by else None,
                "assigned_at": assignment.assigned_at.isoformat() if assignment.assigned_at else None,
            }
            await self._assignment_repository.commit()

            self._log_sync_failure(
                await self._adapter.sync_role_assignment(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    role_id=role_id,
                ),
                role_id,
                "assign_role_to_user",
            )

            return Ok(result_dict)

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
