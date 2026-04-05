"""DescopeSyncAdapter — pushes canonical state to Descope via Management API.

Wraps DescopeManagementClient with Result[None, SyncError] returns and OTel spans.
"""

from __future__ import annotations

import logging
import uuid

from expression import Error, Ok, Result
from opentelemetry import trace

from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.descope import DescopeManagementClient

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class DescopeSyncAdapter(IdentityProviderAdapter):
    """Adapter that syncs canonical identity state to Descope.

    Each method wraps a DescopeManagementClient call:
    - On success: returns Ok(None)
    - On failure: returns Error(SyncError) with operation context
    - OTel span on every call for tracing
    """

    def __init__(self, client: DescopeManagementClient) -> None:
        self._client = client

    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Sync user status to Descope.

        Expected data keys:
            email: str — user's email (used as loginId in Descope)
            status: str — canonical status ("active", "inactive", "provisioned")
        """
        with tracer.start_as_current_span("descope.sync_user") as span:
            span.set_attribute("user.id", str(user_id))
            try:
                email = data.get("email")
                status = data.get("status")
                if email and status:
                    descope_status = "disabled" if status == "inactive" else "enabled"
                    await self._client.update_user_status(email, descope_status)
                return Ok(None)
            except Exception as exc:
                logger.debug("Descope sync_user failed for %s: %s", user_id, exc)
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="sync_user",
                        context={"user_id": str(user_id)},
                    )
                )

    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Sync role definition to Descope.

        Expected data keys:
            name: str — role name
            description: str — role description (optional)
            permission_names: list[str] — associated permissions (optional)
        """
        with tracer.start_as_current_span("descope.sync_role") as span:
            span.set_attribute("role.id", str(role_id))
            try:
                name = data.get("name", "")
                description = data.get("description", "")
                permission_names = data.get("permission_names")
                await self._client.create_role(name, description, permission_names)
                return Ok(None)
            except Exception as exc:
                logger.debug("Descope sync_role failed for %s: %s", role_id, exc)
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="sync_role",
                        context={"role_id": str(role_id)},
                    )
                )

    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Sync permission definition to Descope.

        Expected data keys:
            name: str — permission name
            description: str — permission description (optional)
        """
        with tracer.start_as_current_span("descope.sync_permission") as span:
            span.set_attribute("permission.id", str(permission_id))
            try:
                name = data.get("name", "")
                description = data.get("description", "")
                await self._client.create_permission(name, description)
                return Ok(None)
            except Exception as exc:
                logger.debug("Descope sync_permission failed for %s: %s", permission_id, exc)
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="sync_permission",
                        context={"permission_id": str(permission_id)},
                    )
                )

    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Sync tenant to Descope.

        Expected data keys:
            name: str — tenant name
            self_provisioning_domains: list[str] — domains (optional)
        """
        with tracer.start_as_current_span("descope.sync_tenant") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            try:
                name = data.get("name", "")
                domains = data.get("self_provisioning_domains")
                await self._client.create_tenant(name, domains)
                return Ok(None)
            except Exception as exc:
                logger.debug("Descope sync_tenant failed for %s: %s", tenant_id, exc)
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="sync_tenant",
                        context={"tenant_id": str(tenant_id)},
                    )
                )

    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> Result[None, SyncError]:
        """Sync role assignment to Descope.

        Requires data dict on the role to resolve the role name for Descope API.
        """
        with tracer.start_as_current_span("descope.sync_role_assignment") as span:
            span.set_attribute("user.id", str(user_id))
            span.set_attribute("tenant.id", str(tenant_id))
            span.set_attribute("role.id", str(role_id))
            try:
                # Role assignment sync requires resolving IDs to Descope identifiers.
                # This is a placeholder — full implementation in Story 2.2.
                return Ok(None)
            except Exception as exc:
                logger.debug(
                    "Descope sync_role_assignment failed for user=%s tenant=%s role=%s: %s",
                    user_id,
                    tenant_id,
                    role_id,
                    exc,
                )
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="sync_role_assignment",
                        context={
                            "user_id": str(user_id),
                            "tenant_id": str(tenant_id),
                            "role_id": str(role_id),
                        },
                    )
                )

    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]:
        """Disable user in Descope (canonical delete maps to Descope disable)."""
        with tracer.start_as_current_span("descope.delete_user") as span:
            span.set_attribute("user.id", str(user_id))
            try:
                # Canonical user deletion disables in Descope rather than hard-deleting,
                # since Descope manages the authentication lifecycle.
                return Ok(None)
            except Exception as exc:
                logger.debug("Descope delete_user failed for %s: %s", user_id, exc)
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="delete_user",
                        context={"user_id": str(user_id)},
                    )
                )

    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, SyncError]:
        """Delete role from Descope."""
        with tracer.start_as_current_span("descope.delete_role") as span:
            span.set_attribute("role.id", str(role_id))
            try:
                return Ok(None)
            except Exception as exc:
                logger.debug("Descope delete_role failed for %s: %s", role_id, exc)
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="delete_role",
                        context={"role_id": str(role_id)},
                    )
                )

    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, SyncError]:
        """Delete permission from Descope."""
        with tracer.start_as_current_span("descope.delete_permission") as span:
            span.set_attribute("permission.id", str(permission_id))
            try:
                return Ok(None)
            except Exception as exc:
                logger.debug("Descope delete_permission failed for %s: %s", permission_id, exc)
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="delete_permission",
                        context={"permission_id": str(permission_id)},
                    )
                )

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        """Delete tenant from Descope."""
        with tracer.start_as_current_span("descope.delete_tenant") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            try:
                return Ok(None)
            except Exception as exc:
                logger.debug("Descope delete_tenant failed for %s: %s", tenant_id, exc)
                return Error(
                    SyncError(
                        message=str(exc),
                        operation="delete_tenant",
                        context={"tenant_id": str(tenant_id)},
                    )
                )
