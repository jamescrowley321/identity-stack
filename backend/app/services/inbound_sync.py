"""InboundSyncService — domain orchestration for inbound identity sync.

Handles Descope Flow HTTP Connector sign-up sync and webhook event processing.
Middle layer of onion architecture: orchestrates repositories for data access.
All methods return Result[T, IdentityError]. OTel spans on every method.
"""

from __future__ import annotations

import hashlib
import logging

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Conflict, IdentityError, NotFound, ValidationError
from app.models.identity.provider import ProviderType
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.user import RepositoryConflictError, UserRepository
from app.services.cache_invalidation import CacheInvalidationPublisher

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


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
        publisher: CacheInvalidationPublisher | None = None,
    ) -> None:
        self._user_repo = user_repository
        self._link_repo = idp_link_repository
        self._provider_repo = provider_repository
        self._publisher = publisher or CacheInvalidationPublisher()

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

        try:
            await self._user_repo.commit()
        except Exception:
            logger.exception("Commit failed during user.deleted webhook")
            return Error(Conflict(message="Failed to persist user deactivation"))

        await self._publisher.publish(entity_type="user", entity_id=user.id, operation="deactivate")

        logger.info("Webhook: deactivated user %s from user.deleted event", user.id)
        return Ok({"status": "deactivated", "user_id": str(user.id)})
