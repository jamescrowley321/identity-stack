"""IdentityService ABC — canonical interface for all identity domain operations.

All methods return Result[T, IdentityError] — never raise for domain errors.
All tenant-scoped methods take tenant_id: UUID as explicit parameter.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from expression import Result

from app.errors.identity import IdentityError


class IdentityService(ABC):
    """Abstract base class for identity domain operations.

    Concrete implementations (e.g. PostgresIdentityService) inject an
    AsyncSession and an IdentityProviderAdapter via constructor.
    """

    # --- User operations ---

    @abstractmethod
    async def create_user(
        self,
        *,
        tenant_id: uuid.UUID,
        email: str,
        user_name: str,
        given_name: str = "",
        family_name: str = "",
    ) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def get_user(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def update_user(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        email: str | None = None,
        user_name: str | None = None,
        given_name: str | None = None,
        family_name: str | None = None,
    ) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def deactivate_user(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def search_users(
        self, *, tenant_id: uuid.UUID, query: str = "", status: str | None = None
    ) -> Result[list[dict], IdentityError]: ...

    # --- Role operations ---

    @abstractmethod
    async def create_role(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        name: str,
        description: str = "",
    ) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def get_role(self, *, role_id: uuid.UUID) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def update_role(
        self,
        *,
        role_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, IdentityError]: ...

    # --- Permission operations ---

    @abstractmethod
    async def create_permission(
        self,
        *,
        name: str,
        description: str = "",
    ) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def get_permission(self, *, permission_id: uuid.UUID) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def update_permission(
        self,
        *,
        permission_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, IdentityError]: ...

    @abstractmethod
    async def map_permission_to_role(
        self,
        *,
        role_id: uuid.UUID,
        permission_id: uuid.UUID,
    ) -> Result[None, IdentityError]: ...

    @abstractmethod
    async def unmap_permission_from_role(
        self,
        *,
        role_id: uuid.UUID,
        permission_id: uuid.UUID,
    ) -> Result[None, IdentityError]: ...

    # --- Tenant operations ---

    @abstractmethod
    async def create_tenant(
        self,
        *,
        name: str,
        domains: list[str] | None = None,
    ) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def get_tenant(self, *, tenant_id: uuid.UUID) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def update_tenant(
        self,
        *,
        tenant_id: uuid.UUID,
        name: str | None = None,
        domains: list[str] | None = None,
    ) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, IdentityError]: ...

    # --- Role assignment operations ---

    @abstractmethod
    async def assign_role_to_user(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> Result[None, IdentityError]: ...

    @abstractmethod
    async def remove_role_from_user(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
    ) -> Result[None, IdentityError]: ...

    @abstractmethod
    async def get_tenant_users_with_roles(self, *, tenant_id: uuid.UUID) -> Result[list[dict], IdentityError]: ...
