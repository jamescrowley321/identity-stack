"""Unit tests for UserService domain orchestration (Story 2.1).

Tests cover:
- create_user: persist → commit → sync → Ok(user); duplicate email → Conflict; sync failure → warning + Ok
- get_user: found → Ok(dict); not found → NotFound
- update_user: happy path, not found, email conflict with different user
- deactivate_user: sets status=inactive, syncs, not found case
- search_users: delegates to repository with tenant scoping
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from expression import Error, Ok

from app.errors.identity import Conflict, NotFound
from app.models.identity.user import User, UserStatus
from app.repositories.user import RepositoryConflictError, UserRepository
from app.services.adapters.base import IdentityProviderAdapter, SyncError
from app.services.user import UserService

TENANT_ID = uuid.uuid4()


def _make_user(**overrides) -> User:
    """Create a User with sensible defaults."""
    defaults = {
        "id": uuid.uuid4(),
        "email": "alice@example.com",
        "user_name": "alice",
        "given_name": "Alice",
        "family_name": "Smith",
        "status": UserStatus.active,
    }
    defaults.update(overrides)
    return User(**defaults)


def _build_service(
    repo: AsyncMock | None = None,
    adapter: AsyncMock | None = None,
) -> tuple[UserService, AsyncMock, AsyncMock]:
    """Build a UserService with mocked repository and adapter."""
    if repo is None:
        repo = AsyncMock(spec=UserRepository)
    if adapter is None:
        adapter = AsyncMock(spec=IdentityProviderAdapter)
    service = UserService(repository=repo, adapter=adapter)
    return service, repo, adapter


@pytest.mark.anyio
class TestCreateUser:
    """AC-2.1.2: create_user persists via repo, commits, syncs to adapter, returns Ok(dict)."""

    async def test_create_user_success(self):
        service, repo, adapter = _build_service()
        repo.get_by_email.return_value = None
        user = _make_user()
        repo.create.return_value = user
        adapter.sync_user.return_value = Ok(None)

        result = await service.create_user(
            tenant_id=TENANT_ID,
            email=user.email,
            user_name=user.user_name,
            given_name=user.given_name,
            family_name=user.family_name,
        )

        assert result.is_ok()
        repo.create.assert_awaited_once()
        repo.commit.assert_awaited_once()
        adapter.sync_user.assert_awaited_once()

    async def test_create_user_commit_before_sync(self):
        """Commit must happen before sync to prevent ghost state in IdP."""
        service, repo, adapter = _build_service()
        repo.get_by_email.return_value = None
        user = _make_user()
        repo.create.return_value = user
        adapter.sync_user.return_value = Ok(None)

        call_order = []
        repo.commit.side_effect = lambda: call_order.append("commit")
        adapter.sync_user.side_effect = lambda **kw: (call_order.append("sync"), Ok(None))[1]

        await service.create_user(
            tenant_id=TENANT_ID,
            email=user.email,
            user_name=user.user_name,
        )

        assert call_order == ["commit", "sync"]

    async def test_create_user_duplicate_email_returns_conflict(self):
        service, repo, _adapter = _build_service()
        repo.get_by_email.return_value = _make_user()

        result = await service.create_user(
            tenant_id=TENANT_ID,
            email="alice@example.com",
            user_name="alice2",
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        repo.create.assert_not_awaited()

    async def test_create_user_integrity_error_returns_conflict(self):
        """TOCTOU race: get_by_email returns None but flush raises IntegrityError."""
        service, repo, _adapter = _build_service()
        repo.get_by_email.return_value = None
        repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await service.create_user(
            tenant_id=TENANT_ID,
            email="alice@example.com",
            user_name="alice",
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)

    async def test_create_user_sync_failure_still_returns_ok(self):
        """AC-2.1.2: sync failure → log warning, still return Ok(user)."""
        service, repo, adapter = _build_service()
        repo.get_by_email.return_value = None
        user = _make_user()
        repo.create.return_value = user
        adapter.sync_user.return_value = Error(SyncError(message="Descope down", operation="sync_user"))

        with patch("app.services.user.logger") as mock_logger:
            result = await service.create_user(
                tenant_id=TENANT_ID,
                email=user.email,
                user_name=user.user_name,
            )

        assert result.is_ok()
        repo.commit.assert_awaited_once()
        mock_logger.warning.assert_called_once()


@pytest.mark.anyio
class TestGetUser:
    async def test_get_user_found(self):
        service, repo, _adapter = _build_service()
        user = _make_user()
        repo.get_for_tenant.return_value = user

        result = await service.get_user(tenant_id=TENANT_ID, user_id=user.id)

        assert result.is_ok()
        assert result.ok["email"] == user.email
        repo.get_for_tenant.assert_awaited_once_with(user.id, TENANT_ID)

    async def test_get_user_not_found(self):
        service, repo, _adapter = _build_service()
        repo.get_for_tenant.return_value = None

        result = await service.get_user(tenant_id=TENANT_ID, user_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_get_user_wrong_tenant_returns_not_found(self):
        """User exists but has no role in the requested tenant — IDOR prevention."""
        service, repo, _adapter = _build_service()
        repo.get_for_tenant.return_value = None

        result = await service.get_user(tenant_id=TENANT_ID, user_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)


@pytest.mark.anyio
class TestUpdateUser:
    async def test_update_user_success(self):
        service, repo, adapter = _build_service()
        user = _make_user()
        repo.get_for_tenant.return_value = user
        repo.get_by_email.return_value = None
        repo.update.return_value = user
        adapter.sync_user.return_value = Ok(None)

        result = await service.update_user(
            tenant_id=TENANT_ID,
            user_id=user.id,
            email="newemail@example.com",
            given_name="Bob",
        )

        assert result.is_ok()
        repo.get_for_tenant.assert_awaited_once_with(user.id, TENANT_ID)
        repo.update.assert_awaited_once()
        repo.commit.assert_awaited_once()

    async def test_update_user_not_found(self):
        service, repo, _adapter = _build_service()
        repo.get_for_tenant.return_value = None

        result = await service.update_user(tenant_id=TENANT_ID, user_id=uuid.uuid4(), email="x@y.com")

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_update_user_email_conflict_different_user(self):
        service, repo, _adapter = _build_service()
        existing = _make_user(id=uuid.uuid4())
        target = _make_user(id=uuid.uuid4())
        repo.get_for_tenant.return_value = target
        repo.get_by_email.return_value = existing

        result = await service.update_user(
            tenant_id=TENANT_ID,
            user_id=target.id,
            email=existing.email,
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        repo.update.assert_not_awaited()

    async def test_update_user_same_email_no_conflict(self):
        """Updating to the same email the user already has should not conflict."""
        service, repo, adapter = _build_service()
        user = _make_user()
        repo.get_for_tenant.return_value = user
        repo.get_by_email.return_value = user  # same user
        repo.update.return_value = user
        adapter.sync_user.return_value = Ok(None)

        result = await service.update_user(tenant_id=TENANT_ID, user_id=user.id, email=user.email)

        assert result.is_ok()

    async def test_update_user_integrity_error_returns_conflict(self):
        """TOCTOU race: email check passes but flush raises IntegrityError."""
        service, repo, _adapter = _build_service()
        user = _make_user()
        repo.get_for_tenant.return_value = user
        repo.get_by_email.return_value = None
        repo.update.side_effect = RepositoryConflictError("duplicate key")

        result = await service.update_user(
            tenant_id=TENANT_ID,
            user_id=user.id,
            email="new@example.com",
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "conflicts" in result.error.message

    async def test_update_user_commit_failure_returns_error(self):
        """commit() failure should return Error, not raise."""
        service, repo, adapter = _build_service()
        user = _make_user()
        repo.get_for_tenant.return_value = user
        repo.update.return_value = user
        repo.commit.side_effect = Exception("connection lost")

        result = await service.update_user(
            tenant_id=TENANT_ID,
            user_id=user.id,
            given_name="Bob",
        )

        assert result.is_error()
        assert "persist" in result.error.message.lower()


@pytest.mark.anyio
class TestDeactivateUser:
    """AC-2.1.4: deactivate_user sets status=inactive and syncs."""

    async def test_deactivate_user_success(self):
        service, repo, adapter = _build_service()
        user = _make_user(status=UserStatus.active)
        repo.get_for_tenant.return_value = user
        repo.update.return_value = user
        adapter.sync_user.return_value = Ok(None)

        result = await service.deactivate_user(tenant_id=TENANT_ID, user_id=user.id)

        assert result.is_ok()
        assert user.status == UserStatus.inactive
        repo.get_for_tenant.assert_awaited_once_with(user.id, TENANT_ID)
        repo.update.assert_awaited_once()
        repo.commit.assert_awaited_once()

    async def test_deactivate_user_not_found(self):
        service, repo, _adapter = _build_service()
        repo.get_for_tenant.return_value = None

        result = await service.deactivate_user(tenant_id=TENANT_ID, user_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)

    async def test_deactivate_sync_failure_still_returns_ok(self):
        service, repo, adapter = _build_service()
        user = _make_user()
        repo.get_for_tenant.return_value = user
        repo.update.return_value = user
        adapter.sync_user.return_value = Error(SyncError(message="timeout", operation="sync_user"))

        with patch("app.services.user.logger"):
            result = await service.deactivate_user(tenant_id=TENANT_ID, user_id=user.id)

        assert result.is_ok()
        repo.commit.assert_awaited_once()

    async def test_deactivate_conflict_returns_error(self):
        """RepositoryConflictError during deactivate should return Error."""
        service, repo, _adapter = _build_service()
        user = _make_user()
        repo.get_for_tenant.return_value = user
        repo.update.side_effect = RepositoryConflictError("concurrent update")

        result = await service.deactivate_user(tenant_id=TENANT_ID, user_id=user.id)

        assert result.is_error()
        assert isinstance(result.error, Conflict)


@pytest.mark.anyio
class TestSearchUsers:
    """AC-2.1.3: search_users delegates to repository with tenant scoping."""

    async def test_search_returns_list_of_dicts(self):
        service, repo, _adapter = _build_service()
        users = [_make_user(), _make_user(email="bob@example.com", user_name="bob")]
        repo.search.return_value = users
        tenant_id = uuid.uuid4()

        result = await service.search_users(tenant_id=tenant_id, query="")

        assert result.is_ok()
        assert len(result.ok) == 2
        repo.search.assert_awaited_once_with(tenant_id=tenant_id, name=None)

    async def test_search_passes_query_as_name_filter(self):
        service, repo, _adapter = _build_service()
        repo.search.return_value = []
        tenant_id = uuid.uuid4()

        await service.search_users(tenant_id=tenant_id, query="alice")

        repo.search.assert_awaited_once_with(tenant_id=tenant_id, name="alice")

    async def test_search_empty_query_passes_none(self):
        service, repo, _adapter = _build_service()
        repo.search.return_value = []
        tenant_id = uuid.uuid4()

        await service.search_users(tenant_id=tenant_id, query="")

        repo.search.assert_awaited_once_with(tenant_id=tenant_id, name=None)
