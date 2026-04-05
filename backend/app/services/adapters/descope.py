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

    # --- Not yet implemented (stories 2.2+) — pass-through Ok(None) ---

    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        return Ok(None)

    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        return Ok(None)

    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        return Ok(None)

    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)
