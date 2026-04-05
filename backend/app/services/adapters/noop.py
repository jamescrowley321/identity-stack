"""NoOpSyncAdapter — no-op implementation for testing and standalone operation.

All methods return Ok(None) immediately with no external calls.
"""

from __future__ import annotations

import uuid

from expression import Ok, Result

from app.services.adapters.base import IdentityProviderAdapter, SyncError


class NoOpSyncAdapter(IdentityProviderAdapter):
    """Adapter that does nothing — used in tests and when no IdP sync is needed."""

    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        return Ok(None)

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
        role_name: str,
    ) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_role(self, *, role_id: uuid.UUID, role_name: str) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_permission(self, *, permission_id: uuid.UUID, permission_name: str) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)
