"""Unit tests for InboundSyncService domain orchestration (Story 3.1).

Tests cover:
- sync_user_from_flow: new user creation, existing link update, email-only upsert,
  validation errors, provider not found, email conflict, name splitting, duplicate link
- process_webhook_event: user.created delegation, user.updated, user.deleted deactivation,
  unknown event type ignored, missing fields skipped
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.errors.identity import Conflict, NotFound, ValidationError
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.user import RepositoryConflictError, UserRepository
from app.services.inbound_sync import InboundSyncService

PROVIDER_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


def _make_provider(**overrides) -> Provider:
    defaults = {"id": PROVIDER_ID, "name": "descope", "type": ProviderType.descope}
    defaults.update(overrides)
    return Provider(**defaults)


def _make_user(**overrides) -> User:
    defaults = {
        "id": USER_ID,
        "email": "alice@example.com",
        "user_name": "alice@example.com",
        "given_name": "Alice",
        "family_name": "Smith",
        "status": UserStatus.active,
    }
    defaults.update(overrides)
    return User(**defaults)


def _make_link(**overrides) -> IdPLink:
    defaults = {
        "id": uuid.uuid4(),
        "user_id": USER_ID,
        "provider_id": PROVIDER_ID,
        "external_sub": "descope-user-123",
        "external_email": "alice@example.com",
    }
    defaults.update(overrides)
    return IdPLink(**defaults)


def _build_service(
    user_repo: AsyncMock | None = None,
    link_repo: AsyncMock | None = None,
    provider_repo: AsyncMock | None = None,
) -> tuple[InboundSyncService, AsyncMock, AsyncMock, AsyncMock]:
    if user_repo is None:
        user_repo = AsyncMock(spec=UserRepository)
    if link_repo is None:
        link_repo = AsyncMock(spec=IdPLinkRepository)
    if provider_repo is None:
        provider_repo = AsyncMock(spec=ProviderRepository)
    service = InboundSyncService(
        user_repository=user_repo,
        idp_link_repository=link_repo,
        provider_repository=provider_repo,
    )
    return service, user_repo, link_repo, provider_repo


@pytest.mark.anyio
class TestSyncUserFromFlow:
    """AC-3.1.1: sync_user_from_flow creates/updates canonical user + IdP link."""

    async def test_new_user_created(self):
        """New user + IdP link created when no existing link or user."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None
        user_repo.get_by_email.return_value = None
        new_user = _make_user()
        user_repo.create.return_value = new_user

        result = await service.sync_user_from_flow(
            user_id="descope-user-123",
            email="alice@example.com",
            name="Alice Smith",
        )

        assert result.is_ok()
        assert result.ok["created"] is True
        user_repo.create.assert_awaited_once()
        link_repo.create.assert_awaited_once()
        user_repo.commit.assert_awaited_once()

    async def test_existing_link_updates_user(self):
        """Existing IdP link → update existing user, return created=False."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        existing_link = _make_link()
        link_repo.get_by_provider_and_sub.return_value = existing_link
        existing_user = _make_user()
        user_repo.get.return_value = existing_user

        result = await service.sync_user_from_flow(
            user_id="descope-user-123",
            email="new-email@example.com",
            given_name="Bob",
            family_name="Jones",
        )

        assert result.is_ok()
        assert result.ok["created"] is False
        user_repo.update.assert_awaited_once()
        user_repo.commit.assert_awaited_once()
        # Should NOT create a new link
        link_repo.create.assert_not_awaited()

    async def test_existing_user_by_email_creates_link(self):
        """User exists by email but no IdP link → create link, return created=False."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None
        existing_user = _make_user()
        user_repo.get_by_email.return_value = existing_user

        result = await service.sync_user_from_flow(
            user_id="descope-user-456",
            email="alice@example.com",
        )

        assert result.is_ok()
        assert result.ok["created"] is False
        link_repo.create.assert_awaited_once()
        user_repo.create.assert_not_awaited()
        user_repo.commit.assert_awaited_once()

    async def test_missing_email_returns_validation_error(self):
        service, _, _, _ = _build_service()

        result = await service.sync_user_from_flow(user_id="u1", email="")

        assert result.is_error()
        assert isinstance(result.error, ValidationError)
        assert "Email" in result.error.message

    async def test_missing_user_id_returns_validation_error(self):
        service, _, _, _ = _build_service()

        result = await service.sync_user_from_flow(user_id="", email="a@b.com")

        assert result.is_error()
        assert isinstance(result.error, ValidationError)
        assert "user_id" in result.error.message

    async def test_provider_not_found_returns_not_found(self):
        service, _, _, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = None

        result = await service.sync_user_from_flow(
            user_id="u1",
            email="a@b.com",
        )

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "provider" in result.error.message.lower()

    async def test_name_splitting_from_full_name(self):
        """When given_name/family_name absent, split 'name' into parts."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None
        user_repo.get_by_email.return_value = None
        new_user = _make_user()
        user_repo.create.return_value = new_user

        await service.sync_user_from_flow(
            user_id="u1",
            email="a@b.com",
            name="Jane Doe",
        )

        created_user = user_repo.create.call_args[0][0]
        assert created_user.given_name == "Jane"
        assert created_user.family_name == "Doe"

    async def test_name_splitting_single_name(self):
        """Single-word name → given_name only, family_name empty."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None
        user_repo.get_by_email.return_value = None
        new_user = _make_user()
        user_repo.create.return_value = new_user

        await service.sync_user_from_flow(
            user_id="u1",
            email="a@b.com",
            name="Cher",
        )

        created_user = user_repo.create.call_args[0][0]
        assert created_user.given_name == "Cher"
        assert created_user.family_name == ""

    async def test_email_conflict_on_create_returns_conflict(self):
        """TOCTOU: get_by_email returns None but create raises IntegrityError."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None
        user_repo.get_by_email.return_value = None
        user_repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await service.sync_user_from_flow(
            user_id="u1",
            email="dup@example.com",
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_duplicate_link_returns_conflict(self):
        """Link creation raises conflict for existing provider+subject pair."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None
        new_user = _make_user()
        user_repo.get_by_email.return_value = None
        user_repo.create.return_value = new_user
        link_repo.create.side_effect = RepositoryConflictError("unique violation")

        result = await service.sync_user_from_flow(
            user_id="u1",
            email="a@b.com",
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_email_conflict_on_update_returns_conflict(self):
        """Existing link path: update user raises conflict."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = _make_link()
        user_repo.get.return_value = _make_user()
        user_repo.update.side_effect = RepositoryConflictError("dup email")

        result = await service.sync_user_from_flow(
            user_id="descope-user-123",
            email="taken@example.com",
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_linked_user_missing_returns_not_found(self):
        """Existing link but linked user row missing → NotFound."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = _make_link()
        user_repo.get.return_value = None

        result = await service.sync_user_from_flow(
            user_id="descope-user-123",
            email="a@b.com",
        )

        assert result.is_error()
        assert isinstance(result.error, NotFound)


@pytest.mark.anyio
class TestProcessWebhookEvent:
    """AC-3.1.2: process_webhook_event routes by event type, idempotent."""

    async def test_user_created_delegates_to_sync(self):
        """user.created webhook calls sync_user_from_flow under the hood."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None
        user_repo.get_by_email.return_value = None
        new_user = _make_user()
        user_repo.create.return_value = new_user

        result = await service.process_webhook_event(
            event_type="user.created",
            data={"email": "alice@example.com", "user_id": "ext-123", "name": "Alice"},
        )

        assert result.is_ok()
        user_repo.create.assert_awaited_once()

    async def test_user_created_missing_fields_skipped(self):
        """user.created with missing email/user_id → skipped, not error."""
        service, _, _, _ = _build_service()

        result = await service.process_webhook_event(
            event_type="user.created",
            data={"name": "No email or id"},
        )

        assert result.is_ok()
        assert result.ok["status"] == "skipped"

    async def test_user_updated_known_user(self):
        """user.updated for a linked user → update fields, return Ok."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link = _make_link()
        link_repo.get_by_provider_and_sub.return_value = link
        user = _make_user()
        user_repo.get.return_value = user

        result = await service.process_webhook_event(
            event_type="user.updated",
            data={"user_id": "descope-user-123", "email": "updated@example.com"},
        )

        assert result.is_ok()
        assert result.ok["created"] is False
        user_repo.update.assert_awaited_once()
        user_repo.commit.assert_awaited_once()

    async def test_user_updated_unknown_user_ignored(self):
        """user.updated for unknown IdP link → ignored, return Ok."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None

        result = await service.process_webhook_event(
            event_type="user.updated",
            data={"user_id": "unknown-123"},
        )

        assert result.is_ok()
        assert result.ok["status"] == "ignored"
        user_repo.update.assert_not_awaited()

    async def test_user_updated_missing_user_id_skipped(self):
        service, _, _, _ = _build_service()

        result = await service.process_webhook_event(
            event_type="user.updated",
            data={"email": "a@b.com"},
        )

        assert result.is_ok()
        assert result.ok["status"] == "skipped"

    async def test_user_updated_name_splitting(self):
        """user.updated with name but no given/family → splits name."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = _make_link()
        user = _make_user()
        user_repo.get.return_value = user

        await service.process_webhook_event(
            event_type="user.updated",
            data={"user_id": "descope-user-123", "name": "Jane Doe"},
        )

        assert user.given_name == "Jane"
        assert user.family_name == "Doe"

    async def test_user_deleted_deactivates_user(self):
        """user.deleted for a linked user → set status=inactive."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link = _make_link()
        link_repo.get_by_provider_and_sub.return_value = link
        user = _make_user(status=UserStatus.active)
        user_repo.get.return_value = user

        result = await service.process_webhook_event(
            event_type="user.deleted",
            data={"user_id": "descope-user-123"},
        )

        assert result.is_ok()
        assert result.ok["status"] == "deactivated"
        assert user.status == UserStatus.inactive
        user_repo.update.assert_awaited_once()
        user_repo.commit.assert_awaited_once()

    async def test_user_deleted_unknown_user_ignored(self):
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = None

        result = await service.process_webhook_event(
            event_type="user.deleted",
            data={"user_id": "unknown-123"},
        )

        assert result.is_ok()
        assert result.ok["status"] == "ignored"

    async def test_user_deleted_missing_user_id_skipped(self):
        service, _, _, _ = _build_service()

        result = await service.process_webhook_event(
            event_type="user.deleted",
            data={},
        )

        assert result.is_ok()
        assert result.ok["status"] == "skipped"

    async def test_unknown_event_type_ignored(self):
        """Unknown event types → log warning, return success."""
        service, _, _, _ = _build_service()

        result = await service.process_webhook_event(
            event_type="tenant.created",
            data={"tenant_id": "t1"},
        )

        assert result.is_ok()
        assert result.ok["status"] == "ignored"
        assert result.ok["event_type"] == "tenant.created"

    async def test_user_deleted_linked_user_missing_returns_not_found(self):
        """Link exists but linked user row gone → NotFound."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = _make_link()
        user_repo.get.return_value = None

        result = await service.process_webhook_event(
            event_type="user.deleted",
            data={"user_id": "descope-user-123"},
        )

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_user_updated_conflict_returns_error(self):
        """user.updated raising conflict on update → Conflict error."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = _make_link()
        user_repo.get.return_value = _make_user()
        user_repo.update.side_effect = RepositoryConflictError("dup")

        result = await service.process_webhook_event(
            event_type="user.updated",
            data={"user_id": "descope-user-123", "email": "taken@test.com"},
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_user_deleted_conflict_returns_error(self):
        """user.deleted raising conflict on update → Conflict error."""
        service, user_repo, link_repo, provider_repo = _build_service()
        provider_repo.get_by_type.return_value = _make_provider()
        link_repo.get_by_provider_and_sub.return_value = _make_link()
        user_repo.get.return_value = _make_user(status=UserStatus.active)
        user_repo.update.side_effect = RepositoryConflictError("constraint")

        result = await service.process_webhook_event(
            event_type="user.deleted",
            data={"user_id": "descope-user-123"},
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)
