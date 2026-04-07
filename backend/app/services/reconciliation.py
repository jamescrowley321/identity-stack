"""ReconciliationService — domain orchestration for drift detection/resolution.

Detects and resolves drift between canonical Postgres state and Descope.
Middle layer of onion architecture: orchestrates repositories and Descope client.
All methods return Result[T, IdentityError]. OTel spans on every method.

AC-3.2.1: Reconciliation logic (diff + upsert)
AC-3.2.2: Advisory lock for concurrency safety
AC-3.2.3: Descope outage handling (abort on failure)
AC-3.2.4: Performance (batch fetches, OTel spans for visibility)
AC-3.2.5: Failed sync retry (drift detection covers prior failures)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import IdentityError, ProviderError
from app.models.identity.provider import ProviderType
from app.models.identity.role import Permission, Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import RepositoryConflictError, UserRepository
from app.services.descope import DescopeManagementClient

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Descope status → canonical UserStatus mapping
_DESCOPE_STATUS_MAP: dict[str, UserStatus] = {
    "enabled": UserStatus.active,
    "disabled": UserStatus.inactive,
    "invited": UserStatus.provisioned,
}


class ReconciliationService:
    """Domain service for drift detection and resolution between Postgres and Descope.

    Orchestrates repositories for canonical state and DescopeManagementClient for
    Descope state. No direct SQLAlchemy imports — advisory lock injected via callable.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        acquire_lock: Callable[[], Coroutine[Any, Any, None]],
        descope_client: DescopeManagementClient,
        user_repository: UserRepository,
        role_repository: RoleRepository,
        permission_repository: PermissionRepository,
        tenant_repository: TenantRepository,
        idp_link_repository: IdPLinkRepository,
        provider_repository: ProviderRepository,
    ) -> None:
        self._session = session
        self._acquire_lock = acquire_lock
        self._descope = descope_client
        self._user_repo = user_repository
        self._role_repo = role_repository
        self._permission_repo = permission_repository
        self._tenant_repo = tenant_repository
        self._link_repo = idp_link_repository
        self._provider_repo = provider_repository

    async def run(self) -> Result[dict, IdentityError]:
        """Execute a full reconciliation pass.

        AC-3.2.2: Acquires a Postgres advisory lock to prevent concurrent runs.
        AC-3.2.3: Aborts without modifying canonical state on Descope fetch failure.
        """
        with tracer.start_as_current_span("ReconciliationService.run") as span:
            # AC-3.2.2: Advisory lock — prevents concurrent reconciliation
            try:
                await self._acquire_lock()
            except Exception as exc:
                logger.error("Failed to acquire advisory lock: %s", exc)
                return Error(ProviderError(message=f"Failed to acquire reconciliation lock: {exc}"))

            span.set_attribute("reconciliation.started_at", datetime.now(timezone.utc).isoformat())

            # AC-3.2.3: Fetch all Descope state — abort on any failure
            try:
                descope_users = await self._descope.search_all_users()
                descope_roles = await self._descope.list_roles()
                descope_permissions = await self._descope.list_permissions()
                descope_tenants = await self._descope.list_tenants()
            except Exception as exc:
                logger.error("Descope fetch failed — aborting reconciliation: %s", exc)
                span.set_attribute("reconciliation.status", "aborted")
                span.set_attribute("reconciliation.error", str(exc))
                return Error(ProviderError(message=f"Descope API unavailable: {exc}"))

            span.set_attribute("descope.user_count", len(descope_users))
            span.set_attribute("descope.role_count", len(descope_roles))
            span.set_attribute("descope.permission_count", len(descope_permissions))
            span.set_attribute("descope.tenant_count", len(descope_tenants))

            stats: dict[str, int] = {
                "tenants_created": 0,
                "tenants_updated": 0,
                "permissions_created": 0,
                "permissions_updated": 0,
                "roles_created": 0,
                "roles_updated": 0,
                "users_created": 0,
                "users_updated": 0,
                "links_created": 0,
            }

            # Reconcile in dependency order: tenants, permissions, roles, users
            tenant_result = await self._reconcile_tenants(descope_tenants, stats)
            if tenant_result.is_error():
                return tenant_result

            perm_result = await self._reconcile_permissions(descope_permissions, stats)
            if perm_result.is_error():
                return perm_result

            role_result = await self._reconcile_roles(descope_roles, stats)
            if role_result.is_error():
                return role_result

            user_result = await self._reconcile_users(descope_users, stats)
            if user_result.is_error():
                return user_result

            # Commit all changes in a single transaction
            try:
                await self._session.commit()
            except Exception as exc:
                logger.error("Commit failed during reconciliation: %s", exc)
                await self._session.rollback()
                return Error(ProviderError(message=f"Failed to commit reconciliation: {exc}"))

            total_changes = sum(stats.values())
            span.set_attribute("reconciliation.status", "completed")
            span.set_attribute("reconciliation.total_changes", total_changes)

            logger.info("Reconciliation completed: %s", stats)
            return Ok({"status": "completed", "stats": stats})

    async def _reconcile_tenants(
        self,
        descope_tenants: list[dict],
        stats: dict[str, int],
    ) -> Result[None, IdentityError]:
        """Diff Descope tenants against canonical and upsert."""
        with tracer.start_as_current_span("ReconciliationService._reconcile_tenants"):
            canonical_tenants = await self._tenant_repo.list_all()
            canonical_by_name = {t.name: t for t in canonical_tenants}

            for dt in descope_tenants:
                name = dt.get("name", "")
                if not name:
                    continue

                domains = dt.get("selfProvisioningDomains") or []
                existing = canonical_by_name.get(name)

                if existing is None:
                    tenant = Tenant(name=name, domains=domains, status=TenantStatus.active)
                    try:
                        async with self._session.begin_nested():
                            await self._tenant_repo.create(tenant)
                    except RepositoryConflictError:
                        logger.warning("Tenant '%s' conflict during reconciliation — skipping", name)
                        continue
                    stats["tenants_created"] += 1
                    logger.info("Reconciliation: created tenant '%s'", name)
                else:
                    changed = False
                    old_domains = list(existing.domains) if existing.domains else []
                    if existing.domains != domains:
                        existing.domains = domains
                        changed = True
                    if changed:
                        try:
                            async with self._session.begin_nested():
                                await self._tenant_repo.update(existing)
                        except RepositoryConflictError:
                            logger.warning("Tenant '%s' update conflict — skipping", name)
                            continue
                        stats["tenants_updated"] += 1
                        logger.info(
                            "Reconciliation: updated tenant '%s' domains: %s → %s",
                            name,
                            old_domains,
                            domains,
                        )

            return Ok(None)

    async def _reconcile_permissions(
        self,
        descope_permissions: list[dict],
        stats: dict[str, int],
    ) -> Result[None, IdentityError]:
        """Diff Descope permissions against canonical and upsert."""
        with tracer.start_as_current_span("ReconciliationService._reconcile_permissions"):
            canonical_perms = await self._permission_repo.list_all()
            canonical_by_name = {p.name: p for p in canonical_perms}

            for dp in descope_permissions:
                name = dp.get("name", "")
                if not name:
                    continue

                description = dp.get("description", "")
                existing = canonical_by_name.get(name)

                if existing is None:
                    perm = Permission(name=name, description=description)
                    try:
                        async with self._session.begin_nested():
                            await self._permission_repo.create(perm)
                    except RepositoryConflictError:
                        logger.warning("Permission '%s' conflict during reconciliation — skipping", name)
                        continue
                    stats["permissions_created"] += 1
                    logger.info("Reconciliation: created permission '%s'", name)
                else:
                    changed = False
                    old_description = existing.description
                    if existing.description != description:
                        existing.description = description
                        changed = True
                    if changed:
                        try:
                            async with self._session.begin_nested():
                                await self._permission_repo.update(existing)
                        except RepositoryConflictError:
                            logger.warning("Permission '%s' update conflict — skipping", name)
                            continue
                        stats["permissions_updated"] += 1
                        logger.info(
                            "Reconciliation: updated permission '%s' description: '%s' → '%s'",
                            name,
                            old_description,
                            description,
                        )

            return Ok(None)

    async def _reconcile_roles(
        self,
        descope_roles: list[dict],
        stats: dict[str, int],
    ) -> Result[None, IdentityError]:
        """Diff Descope roles against canonical and upsert (global roles only)."""
        with tracer.start_as_current_span("ReconciliationService._reconcile_roles"):
            # Descope roles are global (no tenant scoping)
            canonical_roles = await self._role_repo.list_by_tenant(tenant_id=None)
            canonical_by_name = {r.name: r for r in canonical_roles}

            for dr in descope_roles:
                name = dr.get("name", "")
                if not name:
                    continue

                description = dr.get("description", "")
                existing = canonical_by_name.get(name)

                if existing is None:
                    role = Role(name=name, description=description, tenant_id=None)
                    try:
                        async with self._session.begin_nested():
                            await self._role_repo.create(role)
                    except RepositoryConflictError:
                        logger.warning("Role '%s' conflict during reconciliation — skipping", name)
                        continue
                    stats["roles_created"] += 1
                    logger.info("Reconciliation: created role '%s'", name)
                else:
                    changed = False
                    old_description = existing.description
                    if existing.description != description:
                        existing.description = description
                        changed = True
                    if changed:
                        try:
                            async with self._session.begin_nested():
                                await self._role_repo.update(existing)
                        except RepositoryConflictError:
                            logger.warning("Role '%s' update conflict — skipping", name)
                            continue
                        stats["roles_updated"] += 1
                        logger.info(
                            "Reconciliation: updated role '%s' description: '%s' → '%s'",
                            name,
                            old_description,
                            description,
                        )

            return Ok(None)

    async def _reconcile_users(
        self,
        descope_users: list[dict],
        stats: dict[str, int],
    ) -> Result[None, IdentityError]:
        """Diff Descope users against canonical and upsert with IdP links."""
        with tracer.start_as_current_span("ReconciliationService._reconcile_users"):
            provider = await self._provider_repo.get_by_type(ProviderType.descope)
            if provider is None:
                logger.warning("Descope provider not configured — skipping user reconciliation")
                return Ok(None)

            canonical_users = await self._user_repo.list_all()
            # Use setdefault to keep first entry for each email (avoid silent overwrite)
            canonical_by_email: dict[str, User] = {}
            for u in canonical_users:
                canonical_by_email.setdefault(u.email, u)

            for du in descope_users:
                email = du.get("email", "")
                user_id = du.get("userId", "")
                if not email or not user_id:
                    continue

                name = du.get("name", "")
                given_name = du.get("givenName", "")
                family_name = du.get("familyName", "")
                if not given_name and not family_name and name:
                    parts = name.split(" ", 1)
                    given_name = parts[0]
                    family_name = parts[1] if len(parts) > 1 else ""

                # Map Descope status to canonical (invited → provisioned, not inactive)
                descope_status = du.get("status", "enabled")
                canonical_status = _DESCOPE_STATUS_MAP.get(descope_status, UserStatus.inactive)

                existing = canonical_by_email.get(email)

                if existing is None:
                    user = User(
                        email=email,
                        user_name=email,
                        given_name=given_name,
                        family_name=family_name,
                        status=canonical_status,
                    )
                    try:
                        async with self._session.begin_nested():
                            user = await self._user_repo.create(user)
                    except RepositoryConflictError:
                        logger.warning("User '%s' conflict during reconciliation — skipping", email)
                        continue
                    stats["users_created"] += 1
                    logger.info("Reconciliation: created user '%s' (status=%s)", email, canonical_status.value)
                    existing = user
                else:
                    changes: list[str] = []
                    if given_name and existing.given_name != given_name:
                        changes.append(f"given_name: '{existing.given_name}' → '{given_name}'")
                        existing.given_name = given_name
                    if family_name and existing.family_name != family_name:
                        changes.append(f"family_name: '{existing.family_name}' → '{family_name}'")
                        existing.family_name = family_name
                    if existing.status != canonical_status:
                        changes.append(f"status: '{existing.status.value}' → '{canonical_status.value}'")
                        existing.status = canonical_status
                    if changes:
                        try:
                            async with self._session.begin_nested():
                                await self._user_repo.update(existing)
                        except RepositoryConflictError:
                            logger.warning("User '%s' update conflict — skipping", email)
                            continue
                        stats["users_updated"] += 1
                        logger.info("Reconciliation: updated user '%s': %s", email, ", ".join(changes))

                # Ensure IdP link exists
                existing_link = await self._link_repo.get_by_provider_and_sub(
                    provider_id=provider.id, external_sub=user_id
                )
                if existing_link is None:
                    link = IdPLink(
                        user_id=existing.id,
                        provider_id=provider.id,
                        external_sub=user_id,
                        external_email=email,
                    )
                    try:
                        async with self._session.begin_nested():
                            await self._link_repo.create(link)
                    except RepositoryConflictError:
                        logger.warning("IdP link conflict for user '%s' — skipping", email)
                        continue
                    stats["links_created"] += 1
                    logger.info("Reconciliation: created IdP link for user '%s'", email)

            return Ok(None)
