"""TenantService — domain orchestration for canonical Tenant operations.

Middle layer of onion architecture: orchestrates TenantRepository (data access)
and IdentityProviderAdapter (IdP sync). All methods return Result[T, IdentityError].
OTel spans on every method. Sync failure -> log warning, still return Ok.
"""

from __future__ import annotations

import logging
import uuid

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, IdentityError, NotFound
from app.models.identity.tenant import Tenant
from app.repositories.base import RepositoryConflictError
from app.repositories.tenant import TenantRepository
from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.cache_invalidation import CacheInvalidationPublisher

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class TenantService:
    """Domain service for Tenant operations.

    Orchestrates TenantRepository (inner) and IdentityProviderAdapter (outer).
    Contains NO direct SQLAlchemy imports — uses repository methods only.
    """

    def __init__(
        self,
        *,
        repository: TenantRepository,
        adapter: IdentityProviderAdapter,
        publisher: CacheInvalidationPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._adapter = adapter
        self._publisher = CacheInvalidationPublisher() if publisher is None else publisher

    async def create_tenant(
        self,
        *,
        name: str,
        domains: list[str] | None = None,
    ) -> Result[dict, IdentityError]:
        """Create a new tenant: persist via repo, then sync to IdP.

        AC-2.2.3: tenant CRUD via TenantRepository + adapter.sync_tenant().
        """
        with tracer.start_as_current_span("TenantService.create_tenant") as span:
            span.set_attribute("tenant.name", name)

            existing = await self._repository.get_by_name(name)
            if existing is not None:
                return Error(Conflict(message=f"Tenant '{name}' already exists"))

            tenant = Tenant(name=name, domains=domains or [])
            try:
                tenant = await self._repository.create(tenant)
            except RepositoryConflictError:
                await self._repository.rollback()
                return Error(Conflict(message=f"Tenant '{name}' already exists"))

            result_dict = tenant.model_dump()
            tenant_id = tenant.id
            await self._repository.commit()

            await self._publisher.publish(entity_type="tenant", entity_id=tenant_id, operation="create")

            self._log_sync_failure(
                await self._adapter.sync_tenant(
                    tenant_id=tenant_id,
                    data={
                        "name": tenant.name,
                        "self_provisioning_domains": tenant.domains,
                    },
                ),
                tenant_id,
                "create_tenant",
            )

            return Ok(result_dict)

    async def get_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
    ) -> Result[dict, IdentityError]:
        """Retrieve a tenant by ID."""
        with tracer.start_as_current_span("TenantService.get_tenant") as span:
            span.set_attribute("tenant.id", str(tenant_id))

            tenant = await self._repository.get(tenant_id)
            if tenant is None:
                return Error(NotFound(message=f"Tenant '{tenant_id}' not found"))
            return Ok(tenant.model_dump())

    async def get_tenant_users_with_roles(
        self,
        *,
        tenant_id: uuid.UUID,
    ) -> Result[list[dict], IdentityError]:
        """Get all users and their roles for a tenant.

        AC-2.2.3: 3-way JOIN users <-> user_tenant_roles <-> roles.
        """
        with tracer.start_as_current_span("TenantService.get_tenant_users_with_roles") as span:
            span.set_attribute("tenant.id", str(tenant_id))

            tenant = await self._repository.get(tenant_id)
            if tenant is None:
                return Error(NotFound(message=f"Tenant '{tenant_id}' not found"))

            rows = await self._repository.get_users_with_roles(tenant_id)

            users_map: dict[str, dict] = {}
            for user, role in rows:
                uid = str(user.id)
                if uid not in users_map:
                    users_map[uid] = {
                        "user_id": uid,
                        "email": user.email,
                        "user_name": user.user_name,
                        "given_name": user.given_name,
                        "family_name": user.family_name,
                        "roles": [],
                    }
                users_map[uid]["roles"].append({"role_id": str(role.id), "name": role.name})

            return Ok(list(users_map.values()))

    @staticmethod
    def _log_sync_failure(
        result: Result[None, SyncError],
        entity_id: uuid.UUID,
        operation: str,
    ) -> None:
        """Log adapter sync failures as warnings without propagating."""
        if result.is_error():
            logger.warning(
                "IdP sync failed for tenant %s (%s): %s",
                entity_id,
                operation,
                result.error.message,
            )
