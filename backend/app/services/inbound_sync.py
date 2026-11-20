"""InboundSyncService — domain orchestration for inbound identity sync.

Handles Descope Flow HTTP Connector sign-up sync and webhook event processing,
and the changes the Keycloak event pull reads (``apply_keycloak_change``).
Middle layer of onion architecture: orchestrates repositories for data access.
All methods return Result[T, IdentityError]. OTel spans on every method.
"""

from __future__ import annotations

import hashlib
import uuid as uuid_mod
from dataclasses import dataclass

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, EmailLinkRefused, IdentityError, NotFound, ProviderError, ValidationError
from app.logbook import logbook
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import ProviderType
from app.models.identity.role import Permission, Role
from app.models.identity.sync_event import SyncEvent, SyncEventVerb
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.base import RepositoryConflictError
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.cache_invalidation import CacheInvalidationPublisher
from app.services.identity_provisioning import DEFAULT_ROLE_NAME
from app.services.keycloak_events import (
    AssignmentChanged,
    ClientRoleDeleted,
    ClientRoleRef,
    ClientRoleSaved,
    CompositesChanged,
    KeycloakAccount,
    KeycloakChange,
    MembershipChanged,
    TenantDeleted,
    TenantUpdated,
    UserDeleted,
    UserUpdated,
)

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# The ``detail.source`` of a sync event the Keycloak event pull recorded.
KEYCLOAK_EVENT_SOURCE = "keycloak_event"
# ... and of one a reconciliation pass recorded.
RECONCILIATION_SOURCE = "reconciliation"


def _hash_email(email: str) -> str:
    """One-way hash for OTel span attributes — avoids PII in traces."""
    return hashlib.sha256(email.encode()).hexdigest()[:12]


class InboundSyncService:
    """Domain service for inbound identity synchronisation.

    Orchestrates UserRepository, IdPLinkRepository, and ProviderRepository.
    Contains NO direct SQLAlchemy imports — uses repository methods only.
    """

    def __init__(
        self,
        *,
        user_repository: UserRepository,
        idp_link_repository: IdPLinkRepository,
        provider_repository: ProviderRepository,
        sync_event_repository: SyncEventRepository | None = None,
        publisher: CacheInvalidationPublisher | None = None,
        keycloak_repositories: KeycloakPullRepositories | None = None,
    ) -> None:
        self._user_repo = user_repository
        self._link_repo = idp_link_repository
        self._provider_repo = provider_repository
        self._event_repo = sync_event_repository
        # Only the Keycloak event pull (apply_keycloak_change) reads these.
        self._keycloak = keycloak_repositories
        self._publisher = CacheInvalidationPublisher() if publisher is None else publisher

    async def _record_event(
        self,
        *,
        provider_id: uuid_mod.UUID | None,
        verb: SyncEventVerb,
        subject_type: str,
        subject_id: str = "",
        external_sub: str = "",
        detail: dict | None = None,
    ) -> None:
        if self._event_repo is None:
            return
        try:
            await self._event_repo.create(
                SyncEvent(
                    provider_id=provider_id,
                    verb=verb,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    external_sub=external_sub,
                    detail=detail,
                )
            )
        except Exception:
            logger.exception("Failed to append sync event (verb=%s, subject_type=%s)", verb.value, subject_type)

    async def sync_user_from_flow(
        self,
        *,
        user_id: str,
        email: str,
        name: str | None = None,
        given_name: str | None = None,
        family_name: str | None = None,
    ) -> Result[dict, IdentityError]:
        """Sync a user from Descope Flow HTTP Connector.

        AC-3.1.1: Create/update canonical User + create IdP link.
        Returns 201-equivalent dict (created=True) or 200-equivalent (created=False).
        """
        with tracer.start_as_current_span("InboundSyncService.sync_user_from_flow") as span:
            span.set_attribute("descope.user_id", user_id)
            span.set_attribute("user.email_hash", _hash_email(email))

            if not email:
                return Error(ValidationError(message="Email is required for flow sync"))

            if not user_id:
                return Error(ValidationError(message="user_id is required for flow sync"))

            # Resolve provider
            provider = await self._provider_repo.get_by_type(ProviderType.descope)
            if provider is None:
                return Error(NotFound(message="Descope provider not configured"))

            # Derive name fields
            resolved_given = given_name or ""
            resolved_family = family_name or ""
            if not resolved_given and not resolved_family and name:
                parts = name.split(" ", 1)
                resolved_given = parts[0]
                resolved_family = parts[1] if len(parts) > 1 else ""

            # Check for existing IdP link (idempotency)
            existing_link = await self._link_repo.get_by_provider_and_sub(provider_id=provider.id, external_sub=user_id)

            if existing_link is not None:
                # Update existing user
                existing_user = await self._user_repo.get(existing_link.user_id)
                if existing_user is None:
                    return Error(NotFound(message="Linked user not found for IdP link"))

                if email:
                    existing_user.email = email
                if resolved_given:
                    existing_user.given_name = resolved_given
                if resolved_family:
                    existing_user.family_name = resolved_family

                try:
                    await self._user_repo.update(existing_user)
                except RepositoryConflictError:
                    return Error(Conflict(message=f"Email '{email}' conflicts with another user"))

                await self._record_event(
                    provider_id=provider.id,
                    verb=SyncEventVerb.updated,
                    subject_type="user",
                    subject_id=str(existing_user.id),
                    external_sub=user_id,
                )

                try:
                    await self._user_repo.commit()
                except Exception:
                    logger.exception("Commit failed during flow sync update")
                    return Error(Conflict(message="Failed to persist user update"))

                await self._publisher.publish(entity_type="user", entity_id=existing_user.id, operation="update")

                logger.info("Flow sync: updated existing user %s", existing_user.id)
                return Ok({"user": existing_user.model_dump(), "created": False})

            # Check for existing user by email (upsert)
            existing_user = await self._user_repo.get_by_email(email)
            created = False
            if existing_user is not None and existing_user.email_from_organization:
                # An organization's administrators chose that email; it does not
                # show this Descope identity is that user (IdentityProvisioningService).
                # The response is generic (result_to_response): the holder's id,
                # never the email, is logged so an operator can find it.
                logger.warning(
                    "flow sync refused: provider %s subject %s has the email of canonical user %s, "
                    "whose email an organization chose",
                    provider.name,
                    user_id,
                    existing_user.id,
                )
                return Error(EmailLinkRefused(message="The email is held by a canonical user this login may not link"))

            if existing_user is None:
                # Create new user
                new_user = User(
                    email=email,
                    user_name=email,
                    given_name=resolved_given,
                    family_name=resolved_family,
                    status=UserStatus.active,
                )
                try:
                    new_user = await self._user_repo.create(new_user)
                except RepositoryConflictError:
                    return Error(Conflict(message=f"User with email '{email}' already exists"))
                existing_user = new_user
                created = True

            # Create IdP link
            link = IdPLink(
                user_id=existing_user.id,
                provider_id=provider.id,
                external_sub=user_id,
                external_email=email,
            )
            try:
                await self._link_repo.create(link)
            except RepositoryConflictError:
                return Error(Conflict(message="IdP link already exists for provider/subject"))

            await self._record_event(
                provider_id=provider.id,
                verb=SyncEventVerb.created if created else SyncEventVerb.linked,
                subject_type="user",
                subject_id=str(existing_user.id),
                external_sub=user_id,
            )

            try:
                await self._user_repo.commit()
            except Exception:
                logger.exception("Commit failed during flow sync create/link")
                return Error(Conflict(message="Failed to persist user and link"))

            await self._publisher.publish(
                entity_type="user",
                entity_id=existing_user.id,
                operation="create" if created else "update",
            )

            action = "created" if created else "linked"
            logger.info("Flow sync: %s user %s", action, existing_user.id)
            return Ok({"user": existing_user.model_dump(), "created": created})

    async def process_webhook_event(
        self,
        *,
        event_type: str,
        data: dict,
    ) -> Result[dict, IdentityError]:
        """Process a Descope audit webhook event.

        AC-3.1.2: Routes event by type, idempotent processing.
        Unknown event types → log warning, return success.
        """
        with tracer.start_as_current_span("InboundSyncService.process_webhook_event") as span:
            span.set_attribute("webhook.event_type", event_type)

            handler_map = {
                "user.created": self._handle_user_created,
                "user.updated": self._handle_user_updated,
                "user.deleted": self._handle_user_deleted,
            }

            handler = handler_map.get(event_type)
            if handler is None:
                logger.warning("Unknown webhook event type: %s — ignoring", event_type)
                return Ok({"status": "ignored", "event_type": event_type})

            return await handler(data)

    async def _handle_user_created(self, data: dict) -> Result[dict, IdentityError]:
        """Handle user.created webhook — upsert user + create IdP link."""
        email = data.get("email", "")
        external_sub = data.get("user_id", "")

        if not isinstance(email, str) or not isinstance(external_sub, str) or not email or not external_sub:
            logger.warning("user.created webhook missing email or user_id, keys=%s", list(data.keys()))
            return Ok({"status": "skipped", "reason": "missing required fields"})

        name = data.get("name")
        given_name = data.get("given_name")
        family_name = data.get("family_name")

        return await self.sync_user_from_flow(
            user_id=external_sub,
            email=email,
            name=name if isinstance(name, str) else None,
            given_name=given_name if isinstance(given_name, str) else None,
            family_name=family_name if isinstance(family_name, str) else None,
        )

    async def _handle_user_updated(self, data: dict) -> Result[dict, IdentityError]:
        """Handle user.updated webhook — update user fields if linked."""
        external_sub = data.get("user_id", "")
        if not isinstance(external_sub, str) or not external_sub:
            logger.warning("user.updated webhook missing user_id, keys=%s", list(data.keys()))
            return Ok({"status": "skipped", "reason": "missing user_id"})

        provider = await self._provider_repo.get_by_type(ProviderType.descope)
        if provider is None:
            return Error(NotFound(message="Descope provider not configured"))

        link = await self._link_repo.get_by_provider_and_sub(provider_id=provider.id, external_sub=external_sub)
        if link is None:
            logger.info("user.updated webhook for unknown user %s — ignoring", external_sub)
            return Ok({"status": "ignored", "reason": "no linked user"})

        user = await self._user_repo.get(link.user_id)
        if user is None:
            return Error(NotFound(message="Linked user not found"))

        email = data.get("email")
        if isinstance(email, str) and email:
            user.email = email
            link.external_email = email
        name = data.get("name")
        given_name = data.get("given_name")
        family_name = data.get("family_name")
        if isinstance(given_name, str) and given_name:
            user.given_name = given_name
        if isinstance(family_name, str) and family_name:
            user.family_name = family_name
        if not given_name and not family_name and isinstance(name, str) and name:
            parts = name.split(" ", 1)
            user.given_name = parts[0]
            user.family_name = parts[1] if len(parts) > 1 else ""

        try:
            await self._user_repo.update(user)
        except RepositoryConflictError:
            return Error(Conflict(message="User update conflicts with existing data"))

        try:
            await self._user_repo.commit()
        except Exception:
            logger.exception("Commit failed during user.updated webhook")
            return Error(Conflict(message="Failed to persist user update"))

        await self._publisher.publish(entity_type="user", entity_id=user.id, operation="update")

        logger.info("Webhook: updated user %s from user.updated event", user.id)
        return Ok({"user": user.model_dump(), "created": False})

    async def _handle_user_deleted(self, data: dict) -> Result[dict, IdentityError]:
        """Handle user.deleted webhook — deactivate user if linked.

        Deactivates rather than deletes to preserve audit trail.
        FK cascade on idp_links will clean up links if the user is later hard-deleted.
        """
        external_sub = data.get("user_id", "")
        if not isinstance(external_sub, str) or not external_sub:
            logger.warning("user.deleted webhook missing user_id, keys=%s", list(data.keys()))
            return Ok({"status": "skipped", "reason": "missing user_id"})

        provider = await self._provider_repo.get_by_type(ProviderType.descope)
        if provider is None:
            return Error(NotFound(message="Descope provider not configured"))

        link = await self._link_repo.get_by_provider_and_sub(provider_id=provider.id, external_sub=external_sub)
        if link is None:
            logger.info("user.deleted webhook for unknown user %s — ignoring", external_sub)
            return Ok({"status": "ignored", "reason": "no linked user"})

        user = await self._user_repo.get(link.user_id)
        if user is None:
            return Error(NotFound(message="Linked user not found"))

        user.status = UserStatus.inactive
        try:
            await self._user_repo.update(user)
        except RepositoryConflictError:
            return Error(Conflict(message="User deactivation conflicts with existing data"))

        await self._record_event(
            provider_id=provider.id,
            verb=SyncEventVerb.deleted,
            subject_type="user",
            subject_id=str(user.id),
            external_sub=external_sub,
        )

        try:
            await self._user_repo.commit()
        except Exception:
            logger.exception("Commit failed during user.deleted webhook")
            return Error(Conflict(message="Failed to persist user deactivation"))

        await self._publisher.publish(entity_type="user", entity_id=user.id, operation="deactivate")

        logger.info("Webhook: deactivated user %s from user.deleted event", user.id)
        return Ok({"status": "deactivated", "user_id": str(user.id)})

    # --- the Keycloak event pull ------------------------------------------------------

    async def apply_keycloak_change(
        self,
        change: KeycloakChange,
        *,
        provider_id: uuid_mod.UUID,
        keycloak_event_id: str,
        source: str = KEYCLOAK_EVENT_SOURCE,
    ) -> Result[dict, IdentityError]:
        """Apply one change the Keycloak event pull read, in the caller's transaction.

        Each change is found in the canonical model by the ``keycloak`` provider's
        links: a tenant by its organization alias (``external_org_id``), a user by
        their ``idp_links`` row or, for an account the backend created, by its
        username (the canonical user id), a role or permission by the canonical id
        its client role carries, else by its name. A change of an entity the
        canonical model does not map is skipped and logged. The rows are flushed,
        never committed: the pull commits them together with its cursor. A sync
        event is recorded for each change that writes or is skipped, with
        ``source`` (the pull's, or ``RECONCILIATION_SOURCE`` for a change a
        reconciliation pass read). The answer names the cache entries to
        invalidate once the caller has committed.
        """
        with tracer.start_as_current_span("InboundSyncService.apply_keycloak_change") as span:
            span.set_attribute("keycloak.change", type(change).__name__)
            repos = self._keycloak
            if repos is None:
                return Error(
                    ProviderError(
                        message="the Keycloak pull needs the tenant, role, permission and assignment repositories"
                    )
                )
            handlers = {
                TenantUpdated: self._keycloak_tenant_updated,
                TenantDeleted: self._keycloak_tenant_deleted,
                UserUpdated: self._keycloak_user_updated,
                UserDeleted: self._keycloak_user_deleted,
                MembershipChanged: self._keycloak_membership,
                ClientRoleSaved: self._keycloak_client_role_saved,
                ClientRoleDeleted: self._keycloak_client_role_deleted,
                CompositesChanged: self._keycloak_composites,
                AssignmentChanged: self._keycloak_assignment,
            }
            outcome = await handlers[type(change)](change, provider_id, repos)
            if outcome.is_error():
                return Error(outcome.error)
            applied = outcome.ok
            span.set_attribute("keycloak.outcome", _status(applied))
            if applied.verb is not None:
                if applied.verb is SyncEventVerb.skipped:
                    logger.info(
                        "Keycloak pull: event %s (%s) skipped: %s",
                        keycloak_event_id,
                        type(change).__name__,
                        applied.reason,
                    )
                await self._record_event(
                    provider_id=provider_id,
                    verb=applied.verb,
                    subject_type=applied.subject_type,
                    subject_id=applied.subject_id,
                    external_sub=applied.external_sub,
                    detail={
                        "source": source,
                        "keycloak_event_id": keycloak_event_id,
                        "change": type(change).__name__,
                        **({"reason": applied.reason} if applied.reason else {}),
                    },
                )
            return Ok(
                {
                    "status": _status(applied),
                    "subject_type": applied.subject_type,
                    "subject_id": applied.subject_id,
                    "invalidations": [dict(entry) for entry in applied.invalidations],
                }
            )

    async def record_keycloak_failure(
        self, *, provider_id: uuid_mod.UUID, keycloak_event_id: str, change: str, message: str
    ) -> Result[None, IdentityError]:
        """Record that a Keycloak event could not be applied (``failed``), in the caller's transaction."""
        with tracer.start_as_current_span("InboundSyncService.record_keycloak_failure"):
            await self._record_event(
                provider_id=provider_id,
                verb=SyncEventVerb.failed,
                subject_type="keycloak_event",
                subject_id=keycloak_event_id,
                detail={
                    "source": KEYCLOAK_EVENT_SOURCE,
                    "keycloak_event_id": keycloak_event_id,
                    "change": change,
                    "error": message[:500],
                },
            )
            return Ok(None)

    # Every handler below answers what it did; none commits.

    async def _keycloak_tenant(
        self, repos: KeycloakPullRepositories, provider_id: uuid_mod.UUID, alias: str
    ) -> Tenant | None:
        found = await repos.tenant.list_by_external_org_id(provider_id, alias)
        return found[0] if len(found) == 1 else None

    async def _keycloak_user(self, provider_id: uuid_mod.UUID, account: KeycloakAccount) -> User | None:
        """The canonical user of a Keycloak account: by its link, else by a username that is a canonical user id.

        The second is the account the adapter created under the canonical id; a
        canonical user linked to another Keycloak account is not that account's.
        """
        if account.id is not None:
            link = await self._link_repo.get_by_provider_and_sub(provider_id=provider_id, external_sub=account.id)
            if link is not None:
                return await self._user_repo.get(link.user_id)
        if account.username is None or not account.admin_named:
            return None
        try:
            user_id = uuid_mod.UUID(account.username)
        except ValueError:
            return None
        user = await self._user_repo.get(user_id)
        if user is None:
            return None
        links = [link for link in await self._link_repo.get_by_user(user.id) if link.provider_id == provider_id]
        if any(link.external_sub != account.id for link in links):
            return None
        return user

    async def _keycloak_tenant_updated(
        self, change: TenantUpdated, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        tenant = await self._keycloak_tenant(repos, provider_id, change.alias)
        if tenant is None:
            return Ok(_skipped("tenant", change.alias, "no tenant is linked to the organization"))
        changed = False
        if change.name is not None and tenant.name != change.name:
            tenant.name = change.name
            changed = True
        if change.domains is not None and sorted(tenant.domains) != sorted(change.domains):
            tenant.domains = list(change.domains)
            changed = True
        if change.enabled is not None:
            status = TenantStatus.active if change.enabled else TenantStatus.suspended
            if tenant.status != status:
                tenant.status = status
                changed = True
        if not changed:
            return Ok(_unchanged("tenant", str(tenant.id)))
        try:
            await repos.tenant.update(tenant)
        except RepositoryConflictError:
            return Error(Conflict(message="The organization's name is another tenant's"))
        return Ok(
            _Applied(
                SyncEventVerb.updated,
                "tenant",
                str(tenant.id),
                change.alias,
                invalidations=(_invalidate("tenant", tenant.id, "update"),),
            )
        )

    async def _keycloak_tenant_deleted(
        self, change: TenantDeleted, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        """The organization is gone: the tenant is suspended, not deleted, so its records stay."""
        tenant = await self._keycloak_tenant(repos, provider_id, change.alias)
        if tenant is None:
            return Ok(_skipped("tenant", change.alias, "no tenant is linked to the organization"))
        if tenant.status == TenantStatus.suspended:
            return Ok(_unchanged("tenant", str(tenant.id)))
        tenant.status = TenantStatus.suspended
        await repos.tenant.update(tenant)
        return Ok(
            _Applied(
                SyncEventVerb.deleted,
                "tenant",
                str(tenant.id),
                change.alias,
                invalidations=(_invalidate("tenant", tenant.id, "update"),),
            )
        )

    async def _keycloak_user_updated(
        self, change: UserUpdated, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        user = await self._keycloak_user(provider_id, change.account)
        if user is None:
            return Ok(_skipped("user", change.account.id or "", "no canonical user is linked to the account"))
        changed = False
        if change.email is not None and user.email.lower() != change.email.lower():
            # Read before any write: the read flushes, and the new email may be another user's.
            link = (
                await self._link_repo.get_by_provider_and_sub(provider_id=provider_id, external_sub=change.account.id)
                if change.account.id is not None
                else None
            )
            user.email = change.email
            # Keycloak, not the person, chose it: no first login links to this user by it (conventions §28).
            user.email_from_organization = True
            changed = True
            if link is not None:
                link.external_email = change.email
        for field, value in (("given_name", change.given_name), ("family_name", change.family_name)):
            if value is not None and getattr(user, field) != value:
                setattr(user, field, value)
                changed = True
        operation = "update"
        if change.enabled is False and user.status != UserStatus.inactive:
            user.status = UserStatus.inactive
            operation = "deactivate"
            changed = True
        elif change.enabled is True and user.status == UserStatus.inactive:
            user.status = UserStatus.active
            changed = True
        if not changed:
            return Ok(_unchanged("user", str(user.id)))
        try:
            await self._user_repo.update(user)
        except RepositoryConflictError:
            return Error(Conflict(message="The account's email is another canonical user's"))
        return Ok(
            _Applied(
                SyncEventVerb.updated,
                "user",
                str(user.id),
                change.account.id or "",
                invalidations=(_invalidate("user", user.id, operation),),
            )
        )

    async def _keycloak_user_deleted(
        self, change: UserDeleted, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        """The account is gone: the user is deactivated, as for Descope's ``user.deleted``, so the records stay."""
        user = await self._keycloak_user(provider_id, change.account)
        if user is None:
            return Ok(_skipped("user", change.account.id or "", "no canonical user is linked to the account"))
        if user.status == UserStatus.inactive:
            return Ok(_unchanged("user", str(user.id)))
        user.status = UserStatus.inactive
        await self._user_repo.update(user)
        return Ok(
            _Applied(
                SyncEventVerb.deleted,
                "user",
                str(user.id),
                change.account.id or "",
                invalidations=(_invalidate("user", user.id, "deactivate"),),
            )
        )

    async def _keycloak_membership(
        self, change: MembershipChanged, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        """A member added holds the default role when they hold none there; a member removed holds none."""
        tenant = await self._keycloak_tenant(repos, provider_id, change.alias)
        user = await self._keycloak_user(provider_id, change.account)
        if tenant is None or user is None:
            return Ok(_skipped("membership", change.account.id or "", "the organization or the account is not linked"))
        subject = f"{user.id}:{tenant.id}"
        invalidations = (_invalidate("user", user.id, "update"),)
        if not change.added:
            removed = await repos.assignment.delete_by_user_tenant(user.id, tenant.id)
            if not removed:
                return Ok(_unchanged("membership", subject))
            return Ok(
                _Applied(
                    SyncEventVerb.deleted, "membership", subject, change.account.id or "", invalidations=invalidations
                )
            )
        if await repos.assignment.list_by_user_tenant(user.id, tenant.id):
            return Ok(_unchanged("membership", subject))
        role = await repos.role.get_by_name(DEFAULT_ROLE_NAME, None)
        if role is None:
            return Error(NotFound(message=f"The default role '{DEFAULT_ROLE_NAME}' does not exist"))
        await repos.assignment.create(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
        return Ok(
            _Applied(SyncEventVerb.created, "membership", subject, change.account.id or "", invalidations=invalidations)
        )

    async def _keycloak_permission(self, repos: KeycloakPullRepositories, ref: ClientRoleRef) -> Permission | None:
        if ref.permission_id is not None:
            return await repos.permission.get(uuid_mod.UUID(ref.permission_id))
        return await repos.permission.get_by_name(ref.name)

    async def _keycloak_role(self, repos: KeycloakPullRepositories, ref: ClientRoleRef) -> Role | None:
        if ref.role_id is not None:
            return await repos.role.get(uuid_mod.UUID(ref.role_id))
        name, tenant_id = _role_scope(ref.name)
        return await repos.role.get_by_name(name, tenant_id)

    async def _keycloak_client_role_saved(
        self, change: ClientRoleSaved, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        """Create the permission or role, or bring it to the client role's name and description.

        It is found by what the client role was (``previous``), else by what it
        is now, so a change already applied is not applied again.
        """
        # What the client role was decides what it is: a rename does not turn a permission into a role.
        is_permission = (change.previous or change.current).is_permission
        subject_type = "permission" if is_permission else "role"
        if (
            not is_permission
            and change.previous is not None
            and _role_scope(change.previous.name)[1] != _role_scope(change.name)[1]
        ):
            return Ok(_skipped("role", change.name, "a rename cannot move a role to another tenant"))
        find = self._keycloak_permission if is_permission else self._keycloak_role
        found = await find(repos, change.previous) if change.previous is not None else None
        if found is None:
            found = await find(repos, change.current)
        description = change.description
        if found is None:
            if is_permission:
                row: Permission | Role = Permission(name=change.name, description=description or "")
                repository = repos.permission
            else:
                name, tenant_id = _role_scope(change.name)
                if tenant_id is not None and await repos.tenant.get(tenant_id) is None:
                    return Ok(_skipped("role", change.name, "the role names a tenant that does not exist"))
                row = Role(name=name, tenant_id=tenant_id, description=description or "")
                repository = repos.role
            try:
                await repository.create(row)
            except RepositoryConflictError:
                return Error(Conflict(message=f"A {subject_type} named '{change.name}' already exists"))
            return Ok(
                _Applied(
                    SyncEventVerb.created,
                    subject_type,
                    str(row.id),
                    change.name,
                    invalidations=(_invalidate(subject_type, row.id, "create"),),
                )
            )
        name = change.name if is_permission else _role_scope(change.name)[0]
        changed = False
        if found.name != name:
            found.name = name
            changed = True
        if description is not None and found.description != description:
            found.description = description
            changed = True
        if not changed:
            return Ok(_unchanged(subject_type, str(found.id)))
        try:
            await (repos.permission if is_permission else repos.role).update(found)
        except RepositoryConflictError:
            return Error(Conflict(message=f"A {subject_type} named '{change.name}' already exists"))
        return Ok(
            _Applied(
                SyncEventVerb.updated,
                subject_type,
                str(found.id),
                change.name,
                invalidations=(_invalidate(subject_type, found.id, "update"),),
            )
        )

    async def _keycloak_client_role_deleted(
        self, change: ClientRoleDeleted, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        is_permission = change.role.is_permission
        subject_type = "permission" if is_permission else "role"
        found = await (self._keycloak_permission if is_permission else self._keycloak_role)(repos, change.role)
        if found is None:
            return Ok(_skipped(subject_type, change.role.name, f"no canonical {subject_type} has that client role"))
        await (repos.permission if is_permission else repos.role).delete(found.id)
        return Ok(
            _Applied(
                SyncEventVerb.deleted,
                subject_type,
                str(found.id),
                change.role.name,
                invalidations=(_invalidate(subject_type, found.id, "delete"),),
            )
        )

    async def _keycloak_composites(
        self, change: CompositesChanged, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        """Add or remove the role's permissions; a composite that is not a canonical permission is left out."""
        role = None if change.role.is_permission else await self._keycloak_role(repos, change.role)
        if role is None:
            return Ok(_skipped("role", change.role.name, "no canonical role has that client role"))
        held = {permission.id for permission in await repos.role.get_permissions(role.id)}
        changed = False
        for ref in change.permissions:
            permission = await self._keycloak_permission(repos, ref) if ref.is_permission else None
            if permission is None:
                logger.info("Keycloak pull: composite %s of role %s is not a canonical permission", ref.name, role.id)
                continue
            if change.added and permission.id not in held:
                await repos.role.add_permission(role.id, permission.id)
                changed = True
            elif not change.added and permission.id in held:
                await repos.role.remove_permission(role.id, permission.id)
                changed = True
        if not changed:
            return Ok(_unchanged("role", str(role.id)))
        return Ok(
            _Applied(
                SyncEventVerb.updated,
                "role",
                str(role.id),
                change.role.name,
                invalidations=(_invalidate("role", role.id, "update"),),
            )
        )

    async def _keycloak_assignment(
        self, change: AssignmentChanged, provider_id: uuid_mod.UUID, repos: KeycloakPullRepositories
    ) -> Result[_Applied, IdentityError]:
        """Assign or unassign the role the organization group stands for."""
        tenant = await self._keycloak_tenant(repos, provider_id, change.alias)
        user = await self._keycloak_user(provider_id, change.account)
        if change.role_id is not None:
            role = await repos.role.get(uuid_mod.UUID(change.role_id))
        else:
            name, tenant_id = _role_scope(change.group_name)
            role = await repos.role.get_by_name(name, tenant_id)
        if tenant is None or user is None or role is None or role.tenant_id not in (None, tenant.id):
            return Ok(
                _skipped("role_assignment", change.account.id or "", "the organization, account or group is not linked")
            )
        subject = f"{user.id}:{tenant.id}:{role.id}"
        invalidations = (_invalidate("user", user.id, "update"),)
        if change.added:
            if await repos.assignment.get(user.id, tenant.id, role.id) is not None:
                return Ok(_unchanged("role_assignment", subject))
            await repos.assignment.create(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
            return Ok(
                _Applied(
                    SyncEventVerb.created,
                    "role_assignment",
                    subject,
                    change.account.id or "",
                    invalidations=invalidations,
                )
            )
        if not await repos.assignment.delete(user.id, tenant.id, role.id):
            return Ok(_unchanged("role_assignment", subject))
        return Ok(
            _Applied(
                SyncEventVerb.deleted, "role_assignment", subject, change.account.id or "", invalidations=invalidations
            )
        )


@dataclass(frozen=True)
class KeycloakPullRepositories:
    """The repositories ``apply_keycloak_change`` writes besides the user's and the links'."""

    tenant: TenantRepository
    role: RoleRepository
    permission: PermissionRepository
    assignment: UserTenantRoleRepository


@dataclass(frozen=True)
class _Applied:
    """What a Keycloak change did: ``verb`` is the sync event to record, ``None`` for nothing changed."""

    verb: SyncEventVerb | None
    subject_type: str
    subject_id: str
    external_sub: str = ""
    reason: str = ""
    invalidations: tuple[tuple[tuple[str, str], ...], ...] = ()


def _status(applied: _Applied) -> str:
    if applied.verb is None:
        return "unchanged"
    return "skipped" if applied.verb is SyncEventVerb.skipped else "applied"


def _skipped(subject_type: str, external_sub: str, reason: str) -> _Applied:
    return _Applied(SyncEventVerb.skipped, subject_type, "", external_sub, reason)


def _unchanged(subject_type: str, subject_id: str) -> _Applied:
    return _Applied(None, subject_type, subject_id)


def _invalidate(entity_type: str, entity_id: uuid_mod.UUID, operation: str) -> tuple[tuple[str, str], ...]:
    return (("entity_type", entity_type), ("entity_id", str(entity_id)), ("operation", operation))


def _role_scope(client_name: str) -> tuple[str, uuid_mod.UUID | None]:
    """A client role's canonical role name and tenant: ``<name>@<tenant id>`` is a tenant's own role."""
    name, separator, suffix = client_name.rpartition("@")
    if separator and name:
        try:
            return name, uuid_mod.UUID(suffix)
        except ValueError:
            pass
    return client_name, None
