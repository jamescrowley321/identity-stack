"""OrySyncAdapter — outbound canonical→Ory sync.

Greenfield: provisions Ory state as the canonical model changes.

- ``sync_user`` upserts an Ory identity (email + SCIM-aligned traits).
- ``sync_role`` / ``sync_permission`` (and their deletes) are **no-ops** — roles and
  permissions stay canonical-only, like ``NoOpSyncAdapter``.
- ``sync_tenant`` creates an Ory **Organization** only when ``enable_organizations``
  is set; otherwise it is a no-op (canonical-side tenancy — the default, since the
  Develop tier has no Organizations).
- ``sync_role_assignment`` and the ``delete_*`` methods are canonical-authoritative
  no-ops for now (the canonical model remains the source of truth).

The concrete Ory Admin API client is injected (``OryIdentityClient``); a production
httpx implementation is a follow-up. This module defines the contract and the sync
semantics, unit-tested against a mock client — parity with how ``DescopeSyncAdapter``
is tested.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from expression import Error, Ok, Result
from opentelemetry import trace

from app.services.adapters.base import IdentityProviderAdapter, SyncError

tracer = trace.get_tracer(__name__)


class OryIdentityClient(Protocol):
    """Minimal Ory Admin API surface the adapter needs (injected)."""

    async def upsert_identity(self, *, email: str, traits: dict) -> None: ...

    async def upsert_organization(self, *, label: str) -> str: ...


class OrySyncAdapter(IdentityProviderAdapter):
    """Syncs canonical identity state to Ory. Roles/permissions stay canonical."""

    def __init__(self, client: OryIdentityClient, *, enable_organizations: bool = False) -> None:
        self._client = client
        self._enable_organizations = enable_organizations

    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Upsert an Ory identity from canonical user data.

        Expected data keys: email (required), given_name, family_name (optional).
        """
        with tracer.start_as_current_span("ory.sync_user") as span:
            span.set_attribute("user.id", str(user_id))
            email = data.get("email")
            if not email:
                return Error(
                    SyncError(
                        message="Missing required sync data: email is required",
                        operation="sync_user",
                        context={"user_id": str(user_id)},
                    )
                )
            traits: dict = {"email": email}
            for key in ("given_name", "family_name"):
                value = data.get(key)
                if value:
                    traits[key] = value
            try:
                await self._client.upsert_identity(email=email, traits=traits)
                return Ok(None)
            except Exception as exc:
                return Error(SyncError(message=str(exc), operation="sync_user", context={"user_id": str(user_id)}))

    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        # Roles are canonical-only — no-op (like NoOpSyncAdapter).
        return Ok(None)

    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        # Permissions are canonical-only — no-op.
        return Ok(None)

    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create/update an Ory Organization — only when Organizations are enabled.

        Default (``enable_organizations`` False): no-op, tenancy stays canonical-side.
        """
        if not self._enable_organizations:
            return Ok(None)
        with tracer.start_as_current_span("ory.sync_tenant") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            label = data.get("name")
            if not label:
                return Error(
                    SyncError(
                        message="Missing required sync data: name is required",
                        operation="sync_tenant",
                        context={"tenant_id": str(tenant_id)},
                    )
                )
            try:
                await self._client.upsert_organization(label=label)
                return Ok(None)
            except Exception as exc:
                return Error(
                    SyncError(message=str(exc), operation="sync_tenant", context={"tenant_id": str(tenant_id)})
                )

    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]:
        # Role assignments are canonical-authoritative — no-op (optional org
        # membership mirroring is a future enhancement).
        return Ok(None)

    async def delete_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]:
        # Placeholder: deleting the Ory identity needs its external ref, which the
        # adapter does not resolve from a canonical user_id alone.
        return Ok(None)

    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        # Placeholder: deleting the Ory Organization needs its external id.
        return Ok(None)
