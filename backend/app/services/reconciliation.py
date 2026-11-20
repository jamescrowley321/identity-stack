"""ReconciliationService — drift detection and resolution, one pass per configured provider.

Each provider the canonical model is pushed to has a pass that reads that
provider's state and repairs what the push (``app/services/adapters/``) and the
inbound sync missed. ``ReconciliationService`` runs every configured pass in
turn, each in its own transaction, and reports each one. A pass is a
``ProviderReconciliation``: Descope's is ``DescopeReconciliation`` below,
Keycloak's ``app.services.keycloak_reconciliation.KeycloakReconciliation``.

Descope (AC-3.2.x):
AC-3.2.1: Reconciliation logic (diff + upsert)
AC-3.2.2: Advisory lock for concurrency safety
AC-3.2.3: Descope outage handling (abort on failure)
AC-3.2.4: Performance (batch fetches, OTel spans for visibility)
AC-3.2.5: Failed sync retry (drift detection covers prior failures)
"""

from __future__ import annotations

import enum
from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import IdentityError, ProviderError
from app.logbook import logbook
from app.models.identity.provider import ProviderType
from app.models.identity.role import Permission, Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.base import RepositoryConflictError
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_ledger import ReconciliationRunRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.descope import DescopeManagementClient

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# Descope status → canonical UserStatus mapping
_DESCOPE_STATUS_MAP: dict[str, UserStatus] = {
    "enabled": UserStatus.active,
    "disabled": UserStatus.inactive,
    "invited": UserStatus.provisioned,
}


class PassStatus(enum.StrEnum):
    """How a provider's pass ended (``reconciliation_runs.status``)."""

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class PassOutcome:
    """What a pass did: its counts, and how many of them changed the canonical model or the provider."""

    stats: dict[str, int]
    changes: int


class ProviderReconciliation(Protocol):
    """One provider's reconciliation pass. It commits its own changes and never raises for a provider outage."""

    provider: str

    @abstractmethod
    async def reconcile(self) -> Result[PassOutcome, IdentityError]: ...


class ReconciliationService:
    """Runs the reconciliation pass of every configured provider and reports each.

    ``descope_client`` and the repositories build Descope's pass (the arguments
    this service has always taken); ``passes`` are the other providers'. With a
    ``run_repository``, each pass's outcome is recorded (``reconciliation_runs``)
    for ``GET /api/sync/status``.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        acquire_lock: Callable[[], Coroutine[Any, Any, None]],
        descope_client: DescopeManagementClient | None = None,
        user_repository: UserRepository | None = None,
        role_repository: RoleRepository | None = None,
        permission_repository: PermissionRepository | None = None,
        tenant_repository: TenantRepository | None = None,
        idp_link_repository: IdPLinkRepository | None = None,
        provider_repository: ProviderRepository | None = None,
        publisher: CacheInvalidationPublisher | None = None,
        passes: Sequence[ProviderReconciliation] = (),
        run_repository: ReconciliationRunRepository | None = None,
    ) -> None:
        self._session = session
        self._publisher = CacheInvalidationPublisher() if publisher is None else publisher
        self._runs = run_repository
        self._passes: list[ProviderReconciliation] = []
        if descope_client is not None:
            self._passes.append(
                DescopeReconciliation(
                    session=session,
                    acquire_lock=acquire_lock,
                    descope_client=descope_client,
                    user_repository=user_repository or UserRepository(session),
                    role_repository=role_repository or RoleRepository(session),
                    permission_repository=permission_repository or PermissionRepository(session),
                    tenant_repository=tenant_repository or TenantRepository(session),
                    idp_link_repository=idp_link_repository or IdPLinkRepository(session),
                    provider_repository=provider_repository or ProviderRepository(session),
                )
            )
        self._passes.extend(passes)

    async def run(self) -> Result[dict, IdentityError]:
        """Run every configured pass, in order; each commits on its own.

        The answer reports each provider under ``providers``, and ``stats`` sums
        their counts. It is an error only when every pass failed: with one pass,
        that pass's error.
        """
        with tracer.start_as_current_span("ReconciliationService.run") as span:
            span.set_attribute("reconciliation.providers", len(self._passes))
            reports: dict[str, dict] = {}
            totals: dict[str, int] = {}
            errors: list[IdentityError] = []
            for provider_pass in self._passes:
                try:
                    result = await provider_pass.reconcile()
                except Exception as exc:
                    # A pass that raises is that pass failing: the others still run, and it is recorded.
                    logger.error("Reconciliation: the %s pass raised", provider_pass.provider, exc_info=True)
                    await self._session.rollback()
                    result = Error(
                        ProviderError(message=f"The {provider_pass.provider} pass failed: {type(exc).__name__}")
                    )
                if result.is_error():
                    errors.append(result.error)
                    reports[provider_pass.provider] = {"status": PassStatus.FAILED, "error": result.error.message}
                    await self._record(provider_pass.provider, PassStatus.FAILED, {}, result.error.message)
                    continue
                outcome = result.ok
                reports[provider_pass.provider] = {"status": PassStatus.COMPLETED, "stats": outcome.stats}
                for key, value in outcome.stats.items():
                    totals[key] = totals.get(key, 0) + value
                if outcome.changes > 0:
                    await self._publisher.publish_batch(operation="reconcile", stats=outcome.stats)
                await self._record(provider_pass.provider, PassStatus.COMPLETED, outcome.stats, None)
            if errors and len(errors) == len(self._passes):
                span.set_attribute("reconciliation.status", "aborted")
                return Error(errors[0])
            status = "completed" if not errors else "partial"
            span.set_attribute("reconciliation.status", status)
            logger.info("Reconciliation %s: %s", status, totals)
            return Ok({"status": status, "stats": totals, "providers": reports})

    async def _record(self, provider: str, status: PassStatus, stats: dict, error: str | None) -> None:
        """Keep the pass's outcome for the sync status; a failure to keep it is logged, never the pass's."""
        if self._runs is None:
            return
        try:
            await self._session.rollback()
            await self._runs.record(provider, status=status.value, stats=stats, error=error)
            await self._session.commit()
        except Exception:
            logger.warning("Reconciliation: the outcome of the %s pass was not recorded", provider, exc_info=True)
            await self._session.rollback()


class DescopeReconciliation:
    """Descope's pass: makes the canonical model hold Descope's tenants, permissions, roles and users.

    Orchestrates repositories for canonical state and DescopeManagementClient for
    Descope state. No direct SQLAlchemy imports — advisory lock injected via callable.
    """

    provider = "descope"

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

    async def reconcile(self) -> Result[PassOutcome, IdentityError]:
        """Execute a full reconciliation pass.

        AC-3.2.2: Acquires a Postgres advisory lock to prevent concurrent runs.
        AC-3.2.3: Aborts without modifying canonical state on Descope fetch failure.
        """
        with tracer.start_as_current_span("DescopeReconciliation.reconcile") as span:
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

            logger.info("Descope reconciliation completed: %s", stats)
            return Ok(PassOutcome(stats=stats, changes=total_changes))

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
                elif existing.external_org_provider_id is not None:
                    # Linked to a provider's organization: its domains are the
                    # admission list of that organization's first logins, set by
                    # an operator (conventions §28), and a Descope tenant that
                    # shares its name does not choose them. Logged only when
                    # they differ, not for every linked tenant on every run.
                    if existing.domains != domains:
                        logger.info(
                            "Reconciliation: tenant '%s' is linked to an organization, domains left as they are",
                            name,
                        )
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

                if existing is not None and existing.email_from_organization:
                    # A Zitadel organization's administrators chose that email; it
                    # does not show this Descope identity is that user
                    # (IdentityProvisioningService.provision). Neither link nor
                    # update it. Log the holder's id, never the email.
                    logger.warning(
                        "Reconciliation refused: Descope subject %s has the email of canonical user %s, "
                        "whose email an organization chose",
                        user_id,
                        existing.id,
                    )
                    continue

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
