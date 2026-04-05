"""DescopeSyncAdapter — outbound sync to Descope Management API.

Wraps DescopeManagementClient calls in Result types and OTel spans.
Write-through: sync failure is logged but never causes rollback (D7).
"""

from __future__ import annotations

import logging
import uuid

import httpx
from expression import Error, Ok, Result
from opentelemetry import trace

from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.descope import DescopeManagementClient

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class DescopeSyncAdapter(IdentityProviderAdapter):
    """Adapter that syncs canonical state to Descope via the Management API."""

    def __init__(self, client: DescopeManagementClient) -> None:
        self._client = client

    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.sync_user",
            attributes={"user.id": str(user_id)},
        ):
            try:
                status = data.get("status", "active")
                descope_status = "disabled" if status == "inactive" else "enabled"

                # Try to load existing user first; if not found, skip sync
                try:
                    await self._client.load_user(str(user_id))
                    # User exists — update status if needed
                    login_id = await self._client.resolve_login_id(str(user_id))
                    await self._client.update_user_status(login_id, descope_status)
                except httpx.HTTPStatusError as load_err:
                    if load_err.response.status_code == 404:
                        # User doesn't exist in Descope yet — skip sync
                        logger.info(
                            "User %s not found in Descope, skipping sync (will be created on first login)",
                            user_id,
                        )
                    else:
                        raise

                return Ok(None)
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
                err = SyncError(
                    message=str(exc),
                    operation="sync_user",
                    context={"user_id": str(user_id)},
                )
                logger.warning(
                    "Descope sync_user failed: operation=%s user_id=%s error=%s",
                    "sync_user",
                    user_id,
                    exc,
                )
                return Error(err)

    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.delete_user",
            attributes={"user.id": str(user_id)},
        ):
            try:
                try:
                    login_id = await self._client.resolve_login_id(str(user_id))
                    await self._client.update_user_status(login_id, "disabled")
                except httpx.HTTPStatusError as err:
                    if err.response.status_code == 404:
                        logger.info(
                            "User %s not found in Descope, skipping delete",
                            user_id,
                        )
                    else:
                        raise
                return Ok(None)
            except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as exc:
                err = SyncError(
                    message=str(exc),
                    operation="delete_user",
                    context={"user_id": str(user_id)},
                )
                logger.warning(
                    "Descope delete_user failed: operation=%s user_id=%s error=%s",
                    "delete_user",
                    user_id,
                    exc,
                )
                return Error(err)

    # --- Role/Permission/Tenant sync (Story 2.2) ---

    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.sync_role",
            attributes={"role.id": str(role_id)},
        ):
            try:
                name = data.get("name", "")
                description = data.get("description", "")
                old_name = data.get("old_name")
                if old_name and old_name != name:
                    # Name changed — use Descope update API
                    await self._client.update_role(name=old_name, new_name=name, description=description)
                else:
                    await self._client.create_role(name=name, description=description)
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    # Role already exists in Descope — not an error for sync
                    logger.info("Role '%s' already exists in Descope, skipping create", name)
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="sync_role",
                    context={"role_id": str(role_id)},
                )
                logger.warning(
                    "Descope sync_role failed: operation=%s role_id=%s error=%s",
                    "sync_role",
                    role_id,
                    exc,
                )
                return Error(err)
            except httpx.RequestError as exc:
                err = SyncError(
                    message=str(exc),
                    operation="sync_role",
                    context={"role_id": str(role_id)},
                )
                logger.warning(
                    "Descope sync_role failed: operation=%s role_id=%s error=%s",
                    "sync_role",
                    role_id,
                    exc,
                )
                return Error(err)

    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.sync_permission",
            attributes={"permission.id": str(permission_id)},
        ):
            try:
                name = data.get("name", "")
                description = data.get("description", "")
                old_name = data.get("old_name")
                if old_name and old_name != name:
                    # Name changed — use Descope update API
                    await self._client.update_permission(name=old_name, new_name=name, description=description)
                else:
                    await self._client.create_permission(name=name, description=description)
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    logger.info("Permission '%s' already exists in Descope, skipping create", name)
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="sync_permission",
                    context={"permission_id": str(permission_id)},
                )
                logger.warning(
                    "Descope sync_permission failed: operation=%s permission_id=%s error=%s",
                    "sync_permission",
                    permission_id,
                    exc,
                )
                return Error(err)
            except httpx.RequestError as exc:
                err = SyncError(
                    message=str(exc),
                    operation="sync_permission",
                    context={"permission_id": str(permission_id)},
                )
                logger.warning(
                    "Descope sync_permission failed: operation=%s permission_id=%s error=%s",
                    "sync_permission",
                    permission_id,
                    exc,
                )
                return Error(err)

    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.sync_tenant",
            attributes={"tenant.id": str(tenant_id)},
        ):
            try:
                name = data.get("name", "")
                domains = data.get("domains", [])
                await self._client.create_tenant(name=name, self_provisioning_domains=domains or None)
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    logger.info("Tenant '%s' already exists in Descope, skipping create", name)
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="sync_tenant",
                    context={"tenant_id": str(tenant_id)},
                )
                logger.warning(
                    "Descope sync_tenant failed: operation=%s tenant_id=%s error=%s",
                    "sync_tenant",
                    tenant_id,
                    exc,
                )
                return Error(err)
            except httpx.RequestError as exc:
                err = SyncError(
                    message=str(exc),
                    operation="sync_tenant",
                    context={"tenant_id": str(tenant_id)},
                )
                logger.warning(
                    "Descope sync_tenant failed: operation=%s tenant_id=%s error=%s",
                    "sync_tenant",
                    tenant_id,
                    exc,
                )
                return Error(err)

    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        role_name: str,
    ) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.sync_role_assignment",
            attributes={
                "user.id": str(user_id),
                "tenant.id": str(tenant_id),
                "role.id": str(role_id),
            },
        ):
            try:
                # Resolve login ID; Descope API uses role names, not UUIDs
                login_id = await self._client.resolve_login_id(str(user_id))
                await self._client.assign_roles(login_id, str(tenant_id), [role_name])
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info(
                        "User %s or tenant %s not found in Descope, skipping role assignment sync",
                        user_id,
                        tenant_id,
                    )
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="sync_role_assignment",
                    context={
                        "user_id": str(user_id),
                        "tenant_id": str(tenant_id),
                        "role_id": str(role_id),
                    },
                )
                logger.warning(
                    "Descope sync_role_assignment failed: user_id=%s tenant_id=%s role_id=%s error=%s",
                    user_id,
                    tenant_id,
                    role_id,
                    exc,
                )
                return Error(err)
            except (httpx.RequestError, ValueError) as exc:
                err = SyncError(
                    message=str(exc),
                    operation="sync_role_assignment",
                    context={
                        "user_id": str(user_id),
                        "tenant_id": str(tenant_id),
                        "role_id": str(role_id),
                    },
                )
                logger.warning(
                    "Descope sync_role_assignment failed: user_id=%s tenant_id=%s role_id=%s error=%s",
                    user_id,
                    tenant_id,
                    role_id,
                    exc,
                )
                return Error(err)

    async def delete_role(self, *, role_id: uuid.UUID, role_name: str) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.delete_role",
            attributes={"role.id": str(role_id)},
        ):
            try:
                await self._client.delete_role(role_name)
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info("Role %s not found in Descope, skipping delete", role_id)
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="delete_role",
                    context={"role_id": str(role_id)},
                )
                logger.warning(
                    "Descope delete_role failed: operation=%s role_id=%s error=%s",
                    "delete_role",
                    role_id,
                    exc,
                )
                return Error(err)
            except httpx.RequestError as exc:
                err = SyncError(
                    message=str(exc),
                    operation="delete_role",
                    context={"role_id": str(role_id)},
                )
                logger.warning(
                    "Descope delete_role failed: operation=%s role_id=%s error=%s",
                    "delete_role",
                    role_id,
                    exc,
                )
                return Error(err)

    async def delete_permission(self, *, permission_id: uuid.UUID, permission_name: str) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.delete_permission",
            attributes={"permission.id": str(permission_id)},
        ):
            try:
                await self._client.delete_permission(permission_name)
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info("Permission %s not found in Descope, skipping delete", permission_id)
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="delete_permission",
                    context={"permission_id": str(permission_id)},
                )
                logger.warning(
                    "Descope delete_permission failed: operation=%s permission_id=%s error=%s",
                    "delete_permission",
                    permission_id,
                    exc,
                )
                return Error(err)
            except httpx.RequestError as exc:
                err = SyncError(
                    message=str(exc),
                    operation="delete_permission",
                    context={"permission_id": str(permission_id)},
                )
                logger.warning(
                    "Descope delete_permission failed: operation=%s permission_id=%s error=%s",
                    "delete_permission",
                    permission_id,
                    exc,
                )
                return Error(err)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.delete_tenant",
            attributes={"tenant.id": str(tenant_id)},
        ):
            try:
                await self._client.delete_tenant(str(tenant_id))
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info("Tenant %s not found in Descope, skipping delete", tenant_id)
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="delete_tenant",
                    context={"tenant_id": str(tenant_id)},
                )
                logger.warning(
                    "Descope delete_tenant failed: operation=%s tenant_id=%s error=%s",
                    "delete_tenant",
                    tenant_id,
                    exc,
                )
                return Error(err)
            except httpx.RequestError as exc:
                err = SyncError(
                    message=str(exc),
                    operation="delete_tenant",
                    context={"tenant_id": str(tenant_id)},
                )
                logger.warning(
                    "Descope delete_tenant failed: operation=%s tenant_id=%s error=%s",
                    "delete_tenant",
                    tenant_id,
                    exc,
                )
                return Error(err)

    async def invite_user(self, *, email: str, tenant_id: uuid.UUID, role_names: list[str]) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.invite_user",
            attributes={"user.email": email, "tenant.id": str(tenant_id)},
        ):
            try:
                await self._client.invite_user(email, str(tenant_id), role_names)
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                err = SyncError(
                    message=str(exc),
                    operation="invite_user",
                    context={"email": email, "tenant_id": str(tenant_id)},
                )
                logger.warning(
                    "Descope invite_user failed: email=%s tenant_id=%s error=%s",
                    email,
                    tenant_id,
                    exc,
                )
                return Error(err)
            except httpx.RequestError as exc:
                err = SyncError(
                    message=str(exc),
                    operation="invite_user",
                    context={"email": email, "tenant_id": str(tenant_id)},
                )
                logger.warning(
                    "Descope invite_user failed: email=%s tenant_id=%s error=%s",
                    email,
                    tenant_id,
                    exc,
                )
                return Error(err)

    async def remove_user_from_tenant(self, *, user_id: uuid.UUID, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.remove_user_from_tenant",
            attributes={"user.id": str(user_id), "tenant.id": str(tenant_id)},
        ):
            try:
                login_id = await self._client.resolve_login_id(str(user_id))
                await self._client.remove_user_from_tenant(login_id, str(tenant_id))
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info(
                        "User %s not found in Descope, skipping remove_user_from_tenant",
                        user_id,
                    )
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="remove_user_from_tenant",
                    context={"user_id": str(user_id), "tenant_id": str(tenant_id)},
                )
                logger.warning(
                    "Descope remove_user_from_tenant failed: user_id=%s tenant_id=%s error=%s",
                    user_id,
                    tenant_id,
                    exc,
                )
                return Error(err)
            except (httpx.RequestError, ValueError) as exc:
                err = SyncError(
                    message=str(exc),
                    operation="remove_user_from_tenant",
                    context={"user_id": str(user_id), "tenant_id": str(tenant_id)},
                )
                logger.warning(
                    "Descope remove_user_from_tenant failed: user_id=%s tenant_id=%s error=%s",
                    user_id,
                    tenant_id,
                    exc,
                )
                return Error(err)

    async def remove_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        role_name: str,
    ) -> Result[None, SyncError]:
        with tracer.start_as_current_span(
            "descope.remove_role_assignment",
            attributes={
                "user.id": str(user_id),
                "tenant.id": str(tenant_id),
                "role.id": str(role_id),
            },
        ):
            try:
                login_id = await self._client.resolve_login_id(str(user_id))
                await self._client.remove_roles(login_id, str(tenant_id), [role_name])
                return Ok(None)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.info(
                        "User %s or tenant %s not found in Descope, skipping role removal sync",
                        user_id,
                        tenant_id,
                    )
                    return Ok(None)
                err = SyncError(
                    message=str(exc),
                    operation="remove_role_assignment",
                    context={
                        "user_id": str(user_id),
                        "tenant_id": str(tenant_id),
                        "role_id": str(role_id),
                    },
                )
                logger.warning(
                    "Descope remove_role_assignment failed: user_id=%s tenant_id=%s role_id=%s error=%s",
                    user_id,
                    tenant_id,
                    role_id,
                    exc,
                )
                return Error(err)
            except (httpx.RequestError, ValueError) as exc:
                err = SyncError(
                    message=str(exc),
                    operation="remove_role_assignment",
                    context={
                        "user_id": str(user_id),
                        "tenant_id": str(tenant_id),
                        "role_id": str(role_id),
                    },
                )
                logger.warning(
                    "Descope remove_role_assignment failed: user_id=%s tenant_id=%s role_id=%s error=%s",
                    user_id,
                    tenant_id,
                    role_id,
                    exc,
                )
                return Error(err)
