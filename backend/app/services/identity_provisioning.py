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

from app.errors.identity import Conflict, EmailLinkRefused, Forbidden, IdentityError, NotFound, ValidationError
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant, TenantStatus
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
        email_verified: bool = False,
        given_name: str = "",
        family_name: str = "",
        assign_default_role: bool = True,
        organization_id: str | None = None,
    ) -> Result[uuid.UUID, IdentityError]:
        """Ensure a canonical user exists for (provider, sub). Returns the user id.

        Fails closed on identity: an existing link short-circuits (idempotent);
        otherwise a **verified** email is required. Linking a new provider identity
        to an existing canonical user is done only on a verified email — never on a
        self-asserted, unverified one — which is the account-takeover guard. When no
        user with that email exists, a new user is created.

        With ``assign_default_role`` the user is also assigned the default tenant
        and its tenant-scoped default role. A caller whose provider takes its
        grants from the token alone passes ``False``: those grants are never read
        from the canonical model, and a role written for a user it links would be
        held by that user's sessions of providers that do read it.

        With ``organization_id`` (Zitadel's organization claim) the user joins,
        as the global ``member`` role, the one active tenant whose
        ``external_org_id`` is that organization, in place of the default
        tenant. No such tenant, or more than one, is ``Forbidden`` and nothing
        is written: an organization grants a membership only where a canonical
        tenant already names it. Nor is such a login ever linked to an existing
        canonical user by email: that is ``EmailLinkRefused``, and nothing is
        written. An organization's administrators can create a user with any
        email and mark it verified, so on this path a verified email is not
        proof the login is that user's. An administrator links such a user
        beforehand (``scripts/seed_zitadel.py`` writes the ``idp_links`` row).
        """
        with tracer.start_as_current_span(
            "IdentityProvisioningService.provision",
            attributes={"identity.provider": provider_name, "identity.sub": sub},
        ):
            provider = await self._provider_repository.get_by_name(provider_name)
            if provider is None:
                return Error(NotFound(f"Provider '{provider_name}' not found"))
            # Capture the id BEFORE any rollback below. A rolled-back session
            # expires loaded instances, and touching provider.id afterwards would
            # trigger a lazy refresh that raises MissingGreenlet under async.
            provider_id = provider.id

            existing = await self._idp_link_repository.get_by_provider_and_sub(provider_id, sub)
            if existing is not None:
                return Ok(existing.user_id)

            organization_tenant_id: uuid.UUID | None = None
            if organization_id is not None:
                linked = await self.organization_tenant_id(organization_id)
                if linked.is_error():
                    return Error(linked.error)
                organization_tenant_id = linked.ok

            # Account-takeover guard: never create or link on an unverified email.
            # An attacker who asserts a victim's (unverified) email must not be
            # linked to the victim's canonical user.
            if not email:
                return Error(ValidationError("A verified email is required for just-in-time provisioning"))
            if not email_verified:
                return Error(ValidationError("email_verified must be true for just-in-time provisioning"))

            # Retry once to absorb a first-login bootstrap race between two distinct
            # new subjects on the shared default tenant/role (unique names).
            for _ in range(2):
                try:
                    user = await self._user_repository.get_by_email(email)
                    if user is not None and organization_tenant_id is not None:
                        return Error(EmailLinkRefused("The email is held by a canonical user this login may not link"))
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
                            provider_id=provider_id,
                            external_sub=sub,
                            external_email=email,
                        )
                    )
                    await self._session.flush()

                    if assign_default_role and organization_tenant_id is not None:
                        await self._assign_organization_member(user.id, organization_tenant_id)
                    elif assign_default_role:
                        await self._assign_default_role(user.id)

                    await self._session.commit()
                    return Ok(user.id)
                except IntegrityError:
                    await self._session.rollback()
                    # Concurrent same-subject login won the unique
                    # (provider_id, external_sub) race — return the winner's user.
                    existing = await self._idp_link_repository.get_by_provider_and_sub(provider_id, sub)
                    if existing is not None:
                        return Ok(existing.user_id)
                    # Otherwise a bootstrap race on the default tenant/role — retry.
            return Error(Conflict("Could not provision identity after retry; please retry"))

    async def organization_tenant_id(self, organization_id: str) -> Result[uuid.UUID, IdentityError]:
        """The id of the one active tenant linked to ``organization_id``; ``Forbidden`` when there is none.

        Read-only. ``GET /api/identity`` asks it before fetching UserInfo, so a
        token of an unlinked organization costs the provider nothing;
        ``provision`` asks again before it writes.
        Returned as a plain id: a rollback in the retry loop expires loaded rows.
        """
        with tracer.start_as_current_span("IdentityProvisioningService.organization_tenant_id"):
            tenants = await self._tenant_repository.list_by_external_org_id(organization_id)
            if len(tenants) != 1 or tenants[0].status != TenantStatus.active:
                return Error(Forbidden("The organization is not linked to exactly one active tenant"))
            return Ok(tenants[0].id)

    async def _assign_organization_member(self, user_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        """Give the user the global default role in the organization's tenant.

        Global, as the canonical seed's roles are (``scripts/seed_zitadel.py``),
        and created without permissions only when missing.
        """
        role = await self._role_repository.get_by_name(self._default_role_name)
        if role is None:
            role = Role(name=self._default_role_name, description="Default role assigned on just-in-time provisioning")
            self._session.add(role)
            await self._session.flush()
        if await self._assignment_repository.get(user_id, tenant_id, role.id) is None:
            self._session.add(UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=role.id))
            await self._session.flush()

    async def _assign_default_role(self, user_id: uuid.UUID) -> None:
        tenant = await self._get_or_create_tenant(self._default_tenant_name)
        role = await self._get_or_create_role(self._default_role_name, tenant.id)
        if await self._assignment_repository.get(user_id, tenant.id, role.id) is None:
            self._session.add(UserTenantRole(user_id=user_id, tenant_id=tenant.id, role_id=role.id))
            await self._session.flush()

    async def _get_or_create_tenant(self, name: str) -> Tenant:
        tenant = await self._tenant_repository.get_by_name(name)
        if tenant is not None:
            return tenant
        tenant = Tenant(name=name)
        self._session.add(tenant)
        await self._session.flush()
        return tenant

    async def _get_or_create_role(self, name: str, tenant_id: uuid.UUID) -> Role:
        # Tenant-scoped (not global): the default role's permissions apply only
        # within the default tenant, bounding blast radius.
        role = await self._role_repository.get_by_name(name, tenant_id=tenant_id)
        if role is not None:
            return role
        role = Role(
            name=name,
            description="Default role assigned on just-in-time provisioning",
            tenant_id=tenant_id,
        )
        self._session.add(role)
        await self._session.flush()
        return role
