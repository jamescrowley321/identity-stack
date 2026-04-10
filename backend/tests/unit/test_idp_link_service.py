"""Unit tests for IdPLinkService domain orchestration (Story 4.1).

Tests cover:
- create_idp_link: validates user+provider exist, unique constraint → Conflict
- get_user_idp_links: delegates to repository
- delete_idp_link: found → deleted; not found → NotFound
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.errors.identity import Conflict, NotFound
from app.models.identity.user import IdPLink
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.user import RepositoryConflictError, UserRepository
from app.services.idp_link import IdPLinkService


def _make_idp_link(**overrides) -> IdPLink:
    """Create an IdPLink with sensible defaults."""
    defaults = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "provider_id": uuid.uuid4(),
        "external_sub": "ext-sub-123",
        "external_email": "ext@example.com",
        "metadata_": None,
    }
    defaults.update(overrides)
    return IdPLink(**defaults)


def _build_service(
    repo: AsyncMock | None = None,
    user_repo: AsyncMock | None = None,
    provider_repo: AsyncMock | None = None,
) -> tuple[IdPLinkService, AsyncMock, AsyncMock, AsyncMock]:
    """Build an IdPLinkService with mocked repositories."""
    if repo is None:
        repo = AsyncMock(spec=IdPLinkRepository)
    if user_repo is None:
        user_repo = AsyncMock(spec=UserRepository)
    if provider_repo is None:
        provider_repo = AsyncMock(spec=ProviderRepository)
    service = IdPLinkService(
        repository=repo,
        user_repository=user_repo,
        provider_repository=provider_repo,
    )
    return service, repo, user_repo, provider_repo


@pytest.mark.anyio
class TestCreateIdPLink:
    """AC-4.1.1: create_idp_link validates user+provider, enforces unique constraints."""

    async def test_create_success(self):
        service, repo, user_repo, provider_repo = _build_service()
        user_id = uuid.uuid4()
        provider_id = uuid.uuid4()
        link = _make_idp_link(user_id=user_id, provider_id=provider_id)

        user_repo.get.return_value = AsyncMock()  # user exists
        provider_repo.get.return_value = AsyncMock()  # provider exists
        repo.create.return_value = link

        result = await service.create_idp_link(
            user_id=user_id,
            provider_id=provider_id,
            external_sub="ext-sub-123",
            external_email="ext@example.com",
        )

        assert result.is_ok()
        user_repo.get.assert_awaited_once_with(user_id)
        provider_repo.get.assert_awaited_once_with(provider_id)
        repo.create.assert_awaited_once()
        repo.commit.assert_awaited_once()

    async def test_create_user_not_found(self):
        service, repo, user_repo, provider_repo = _build_service()
        user_repo.get.return_value = None

        result = await service.create_idp_link(
            user_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
            external_sub="ext-sub",
        )

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "User" in result.error.message
        repo.create.assert_not_awaited()

    async def test_create_provider_not_found(self):
        service, repo, user_repo, provider_repo = _build_service()
        user_repo.get.return_value = AsyncMock()
        provider_repo.get.return_value = None

        result = await service.create_idp_link(
            user_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
            external_sub="ext-sub",
        )

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Provider" in result.error.message
        repo.create.assert_not_awaited()

    async def test_create_duplicate_returns_conflict(self):
        """Unique constraint violation (user+provider or provider+sub) → Conflict."""
        service, repo, user_repo, provider_repo = _build_service()
        user_repo.get.return_value = AsyncMock()
        provider_repo.get.return_value = AsyncMock()
        repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await service.create_idp_link(
            user_id=uuid.uuid4(),
            provider_id=uuid.uuid4(),
            external_sub="ext-sub",
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        repo.commit.assert_not_awaited()

    async def test_create_with_metadata(self):
        service, repo, user_repo, provider_repo = _build_service()
        user_id = uuid.uuid4()
        provider_id = uuid.uuid4()
        metadata = {"source": "migration", "version": 2}
        link = _make_idp_link(user_id=user_id, provider_id=provider_id, metadata_=metadata)

        user_repo.get.return_value = AsyncMock()
        provider_repo.get.return_value = AsyncMock()
        repo.create.return_value = link

        result = await service.create_idp_link(
            user_id=user_id,
            provider_id=provider_id,
            external_sub="ext-sub",
            metadata=metadata,
        )

        assert result.is_ok()
        assert result.ok["metadata_"] == metadata


@pytest.mark.anyio
class TestGetUserIdPLinks:
    """AC-4.1.1: get_user_idp_links delegates to repository."""

    async def test_get_returns_list(self):
        service, repo, _user_repo, _provider_repo = _build_service()
        user_id = uuid.uuid4()
        links = [
            _make_idp_link(user_id=user_id),
            _make_idp_link(user_id=user_id),
        ]
        repo.get_by_user.return_value = links

        result = await service.get_user_idp_links(user_id=user_id)

        assert result.is_ok()
        assert len(result.ok) == 2
        repo.get_by_user.assert_awaited_once_with(user_id)

    async def test_get_empty_list(self):
        service, repo, _user_repo, _provider_repo = _build_service()
        repo.get_by_user.return_value = []

        result = await service.get_user_idp_links(user_id=uuid.uuid4())

        assert result.is_ok()
        assert result.ok == []


@pytest.mark.anyio
class TestDeleteIdPLink:
    """AC-4.1.1: delete_idp_link removes link; not found → NotFound."""

    async def test_delete_success(self):
        service, repo, _user_repo, _provider_repo = _build_service()
        user_id = uuid.uuid4()
        link_id = uuid.uuid4()
        link = _make_idp_link(id=link_id, user_id=user_id)
        repo.get.return_value = link
        repo.delete.return_value = True

        result = await service.delete_idp_link(link_id=link_id, user_id=user_id)

        assert result.is_ok()
        assert result.ok["status"] == "deleted"
        assert result.ok["link_id"] == str(link_id)
        repo.get.assert_awaited_once_with(link_id)
        repo.delete.assert_awaited_once_with(link_id)
        repo.commit.assert_awaited_once()

    async def test_delete_not_found(self):
        service, repo, _user_repo, _provider_repo = _build_service()
        repo.get.return_value = None

        result = await service.delete_idp_link(link_id=uuid.uuid4(), user_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        repo.delete.assert_not_awaited()
        repo.commit.assert_not_awaited()

    async def test_delete_wrong_user(self):
        """IDOR guard: delete scoped to owning user."""
        service, repo, _user_repo, _provider_repo = _build_service()
        owner_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        link_id = uuid.uuid4()
        link = _make_idp_link(id=link_id, user_id=owner_id)
        repo.get.return_value = link

        result = await service.delete_idp_link(link_id=link_id, user_id=other_user_id)

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        repo.delete.assert_not_awaited()
        repo.commit.assert_not_awaited()
