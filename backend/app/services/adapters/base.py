"""IdentityProviderAdapter ABC — outbound sync interface to external IdPs.

All methods return Result[None, SyncError] — never raise for domain errors.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from expression import Result


@dataclass(frozen=True)
class SyncError:
    """Error returned when an adapter sync operation fails."""

    message: str
    operation: str = ""
    context: dict | None = field(default=None)


class IdentityProviderAdapter(ABC):
    """Abstract base class for outbound identity provider sync operations.

    Implementations push canonical state changes to external IdPs (e.g. Descope).
    """

    @abstractmethod
    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]: ...

    @abstractmethod
    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]: ...

    @abstractmethod
    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]: ...

    @abstractmethod
    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]: ...

    @abstractmethod
    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, SyncError]: ...

    @abstractmethod
    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]: ...
