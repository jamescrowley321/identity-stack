"""IdentityProvisioningService — just-in-time provisioning on first login.

Write-path counterpart to the read-only IdentityResolutionService. When a token
from a JIT-enabled provider (e.g. Ory) resolves to no canonical user, this service
creates the canonical user + idp_link and applies a default tenant/role policy so
greenfield users can use the app without manual provisioning.

Idempotent: the unique (provider_id, external_sub) constraint on idp_links is the
idempotency guarantee — concurrent first requests for the same subject converge on
a single user (the loser re-resolves the winner's link).

All methods return Result[T, IdentityError] and never raise for domain errors.
"""

from __future__ import annotations

import uuid

from expression import Error, Ok, Result
from opentelemetry import trace
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors.identity import Conflict, IdentityError, NotFound, ValidationError
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository

tracer = trace.get_tracer(__name__)

DEFAULT_TENANT_NAME = "default"
DEFAULT_ROLE_NAME = "member"


class IdentityProvisioningService:
    """Just-in-time provisioning of canonical identities for greenfield providers."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        provider_repository: ProviderRepository,
        idp_link_repository: IdPLinkRepository,
        user_repository: UserRepository,
        tenant_repository: TenantRepository,
        role_repository: RoleRepository,
        assignment_repository: UserTenantRoleRepository,
        default_tenant_name: str = DEFAULT_TENANT_NAME,
        default_role_name: str = DEFAULT_ROLE_NAME,
    ) -> None:
        self._session = session
        self._provider_repository = provider_repository
        self._idp_link_repository = idp_link_repository
        self._user_repository = user_repository
        self._tenant_repository = tenant_repository
        self._role_repository = role_repository
        self._assignment_repository = assignment_repository
        self._default_tenant_name = default_tenant_name
        self._default_role_name = default_role_name

    async def provision(
        self,
        *,
        provider_name: str,
        sub: str,
        email: str,
        given_name: str = "",
        family_name: str = "",
    ) -> Result[uuid.UUID, IdentityError]:
        """Ensure a canonical user exists for (provider, sub). Returns the user id.

        If a link already exists, returns its user id (idempotent). Otherwise
        creates the user (or reuses an existing user with the same email), the
        idp_link, and a default tenant/role assignment.
        """
        with tracer.start_as_current_span(
            "IdentityProvisioningService.provision",
            attributes={"identity.provider": provider_name, "identity.sub": sub},
        ):
            provider = await self._provider_repository.get_by_name(provider_name)
            if provider is None:
                return Error(NotFound(f"Provider '{provider_name}' not found"))

            existing = await self._idp_link_repository.get_by_provider_and_sub(provider.id, sub)
            if existing is not None:
                return Ok(existing.user_id)

            if not email:
                return Error(ValidationError("email is required for just-in-time provisioning"))

            try:
                tenant = await self._get_or_create_tenant(self._default_tenant_name)
                role = await self._get_or_create_role(self._default_role_name)

                user = await self._user_repository.get_by_email(email)
                if user is None:
                    user = User(
                        email=email,
                        user_name=email,
                        given_name=given_name,
                        family_name=family_name,
                        status=UserStatus.active,
                    )
                    self._session.add(user)
                    await self._session.flush()

                self._session.add(
                    IdPLink(
                        user_id=user.id,
                        provider_id=provider.id,
                        external_sub=sub,
                        external_email=email,
                    )
                )
                await self._session.flush()

                if await self._assignment_repository.get(user.id, tenant.id, role.id) is None:
                    self._session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
                    await self._session.flush()

                await self._session.commit()
                return Ok(user.id)
            except IntegrityError:
                # Concurrent first login for the same subject won the unique
                # (provider_id, external_sub) race — re-resolve and return it.
                await self._session.rollback()
                existing = await self._idp_link_repository.get_by_provider_and_sub(provider.id, sub)
                if existing is not None:
                    return Ok(existing.user_id)
                return Error(Conflict("Concurrent provisioning conflict; please retry"))

    async def _get_or_create_tenant(self, name: str) -> Tenant:
        tenant = await self._tenant_repository.get_by_name(name)
        if tenant is not None:
            return tenant
        tenant = Tenant(name=name)
        self._session.add(tenant)
        await self._session.flush()
        return tenant

    async def _get_or_create_role(self, name: str) -> Role:
        role = await self._role_repository.get_by_name(name, tenant_id=None)
        if role is not None:
            return role
        role = Role(name=name, description="Default role assigned on just-in-time provisioning")
        self._session.add(role)
        await self._session.flush()
        return role
