"""Unit tests for ProviderService domain orchestration (Stories 4.1 + 4.2).

Tests cover:
- register_provider: happy path, duplicate name → Conflict, integrity error → Conflict
- list_providers: returns providers with config_ref stripped; empty list
- deactivate_provider: found → deactivated; already-inactive → idempotent Ok; not found → NotFound
- get_provider_capabilities: found → capabilities list; not found → NotFound
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.errors.identity import Conflict, NotFound
from app.models.identity.provider import Provider, ProviderType
from app.repositories.base import RepositoryConflictError
from app.repositories.provider import ProviderRepository
from app.services.provider import ProviderService


def _make_provider(**overrides) -> Provider:
    """Create a Provider with sensible defaults."""
    defaults = {
        "id": uuid.uuid4(),
        "name": "descope-prod",
        "type": ProviderType.descope,
        "issuer_url": "https://api.descope.com/P123",
        "base_url": "https://api.descope.com",
        "capabilities": ["sso", "mfa", "rbac"],
        "config_ref": "infisical://descope-prod",
        "active": True,
    }
    defaults.update(overrides)
    return Provider(**defaults)


def _build_service(
    repo: AsyncMock | None = None,
) -> tuple[ProviderService, AsyncMock]:
    """Build a ProviderService with a mocked repository."""
    if repo is None:
        repo = AsyncMock(spec=ProviderRepository)
    service = ProviderService(repository=repo)
    return service, repo


@pytest.mark.anyio
class TestRegisterProvider:
    """AC-4.1.2: register_provider stores config_ref, not credentials."""

    async def test_register_success(self):
        service, repo = _build_service()
        provider = _make_provider()
        repo.get_by_name.return_value = None
        repo.create.return_value = provider

        result = await service.register_provider(
            name=provider.name,
            type=provider.type,
            issuer_url=provider.issuer_url,
            base_url=provider.base_url,
            capabilities=provider.capabilities,
            config_ref=provider.config_ref,
        )

        assert result.is_ok()
        assert result.ok["name"] == provider.name
        assert "config_ref" not in result.ok  # config_ref stripped at service layer
        repo.get_by_name.assert_awaited_once_with(provider.name)
        repo.create.assert_awaited_once()
        repo.commit.assert_awaited_once()

    async def test_register_duplicate_name_returns_conflict(self):
        service, repo = _build_service()
        repo.get_by_name.return_value = _make_provider()

        result = await service.register_provider(
            name="descope-prod",
            type=ProviderType.descope,
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "already exists" in result.error.message
        repo.create.assert_not_awaited()

    async def test_register_integrity_error_returns_conflict(self):
        """TOCTOU race: get_by_name returns None but create raises IntegrityError."""
        service, repo = _build_service()
        repo.get_by_name.return_value = None
        repo.create.side_effect = RepositoryConflictError("duplicate key")

        result = await service.register_provider(
            name="descope-prod",
            type=ProviderType.descope,
        )

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        repo.commit.assert_not_awaited()

    async def test_register_defaults_empty_capabilities(self):
        """Capabilities default to empty list when None is passed."""
        service, repo = _build_service()
        repo.get_by_name.return_value = None
        provider = _make_provider(capabilities=[])
        repo.create.return_value = provider

        result = await service.register_provider(
            name="bare-provider",
            type=ProviderType.oidc,
        )

        assert result.is_ok()
        # Verify the Provider constructor received empty list
        created_arg = repo.create.call_args[0][0]
        assert created_arg.capabilities == []


@pytest.mark.anyio
class TestListProviders:
    """AC-4.2.2: list_providers returns all providers with config_ref stripped."""

    async def test_list_returns_providers_without_config_ref(self):
        service, repo = _build_service()
        providers = [
            _make_provider(name="alpha-provider"),
            _make_provider(name="beta-provider"),
        ]
        repo.list_all.return_value = providers

        result = await service.list_providers()

        assert result.is_ok()
        assert len(result.ok) == 2
        for d in result.ok:
            assert "config_ref" not in d
        repo.list_all.assert_awaited_once()

    async def test_list_empty(self):
        service, repo = _build_service()
        repo.list_all.return_value = []

        result = await service.list_providers()

        assert result.is_ok()
        assert result.ok == []

    async def test_list_preserves_other_fields(self):
        """All fields except config_ref are preserved in the response."""
        service, repo = _build_service()
        provider = _make_provider(
            name="test-provider",
            capabilities=["sso", "mfa"],
            config_ref="infisical://secret",
        )
        repo.list_all.return_value = [provider]

        result = await service.list_providers()

        assert result.is_ok()
        d = result.ok[0]
        assert d["name"] == "test-provider"
        assert d["capabilities"] == ["sso", "mfa"]
        assert "config_ref" not in d


@pytest.mark.anyio
class TestDeactivateProvider:
    """AC-4.1.2: deactivate_provider sets active=False, idempotent for already-inactive."""

    async def test_deactivate_success(self):
        service, repo = _build_service()
        provider = _make_provider(active=True)
        repo.get.return_value = provider
        repo.update.return_value = provider

        result = await service.deactivate_provider(provider_id=provider.id)

        assert result.is_ok()
        assert provider.active is False
        assert "config_ref" not in result.ok  # config_ref stripped at service layer
        repo.update.assert_awaited_once_with(provider)
        repo.commit.assert_awaited_once()

    async def test_deactivate_already_inactive_is_idempotent(self):
        """Deactivating an already-inactive provider short-circuits without writing."""
        service, repo = _build_service()
        provider = _make_provider(active=False)
        repo.get.return_value = provider

        result = await service.deactivate_provider(provider_id=provider.id)

        assert result.is_ok()
        assert provider.active is False
        assert "config_ref" not in result.ok  # config_ref stripped at service layer
        repo.update.assert_not_awaited()
        repo.commit.assert_not_awaited()

    async def test_deactivate_not_found(self):
        service, repo = _build_service()
        repo.get.return_value = None

        result = await service.deactivate_provider(provider_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        repo.update.assert_not_awaited()

    async def test_deactivate_conflict_returns_error(self):
        """RepositoryConflictError on update is caught and returned as Conflict."""
        service, repo = _build_service()
        provider = _make_provider(active=True)
        repo.get.return_value = provider
        repo.update.side_effect = RepositoryConflictError("constraint violation")

        result = await service.deactivate_provider(provider_id=provider.id)

        assert result.is_error()
        assert isinstance(result.error, Conflict)
        repo.commit.assert_not_awaited()


@pytest.mark.anyio
class TestGetProviderCapabilities:
    """AC-4.1.2: get_provider_capabilities returns the capabilities list."""

    async def test_get_capabilities_success(self):
        service, repo = _build_service()
        capabilities = ["sso", "mfa", "rbac"]
        provider = _make_provider(capabilities=capabilities)
        repo.get.return_value = provider

        result = await service.get_provider_capabilities(provider_id=provider.id)

        assert result.is_ok()
        assert result.ok == capabilities
        repo.get.assert_awaited_once_with(provider.id)

    async def test_get_capabilities_empty(self):
        service, repo = _build_service()
        provider = _make_provider(capabilities=[])
        repo.get.return_value = provider

        result = await service.get_provider_capabilities(provider_id=provider.id)

        assert result.is_ok()
        assert result.ok == []

    async def test_get_capabilities_not_found(self):
        service, repo = _build_service()
        repo.get.return_value = None

        result = await service.get_provider_capabilities(provider_id=uuid.uuid4())

        assert result.is_error()
        assert isinstance(result.error, NotFound)
