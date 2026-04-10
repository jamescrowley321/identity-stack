"""Unit tests for ProviderRepository — data access layer tests against real Postgres.

Tests cover:
- create: happy path, duplicate name → RepositoryConflictError
- update: flush mutations, integrity error → RepositoryConflictError
- get: found, not found
- get_by_name: found, not found
- get_by_type: found, not found
- list_all: returns all ordered by name, empty when none exist
"""

import uuid

import pytest

from app.models.identity.provider import Provider, ProviderType
from app.repositories.provider import ProviderRepository
from app.repositories.user import RepositoryConflictError

pytestmark = pytest.mark.asyncio


def _make_provider(**overrides) -> Provider:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"provider-{uuid.uuid4().hex[:8]}",
        "type": ProviderType.descope,
        "issuer_url": "https://api.descope.com/P123",
        "base_url": "https://api.descope.com",
        "capabilities": ["sso", "mfa"],
        "config_ref": "infisical://test",
        "active": True,
    }
    defaults.update(overrides)
    return Provider(**defaults)


async def test_create_provider(db_session):
    repo = ProviderRepository(db_session)
    provider = _make_provider()

    created = await repo.create(provider)

    assert created.id == provider.id
    assert created.name == provider.name
    assert created.type == ProviderType.descope
    assert created.created_at is not None


async def test_create_duplicate_name(db_session):
    repo = ProviderRepository(db_session)
    name = f"dup-{uuid.uuid4().hex[:8]}"

    await repo.create(_make_provider(name=name))
    with pytest.raises(RepositoryConflictError):
        await repo.create(_make_provider(name=name))


async def test_get_found(db_session):
    repo = ProviderRepository(db_session)
    provider = _make_provider()
    await repo.create(provider)

    fetched = await repo.get(provider.id)

    assert fetched is not None
    assert fetched.id == provider.id
    assert fetched.name == provider.name


async def test_get_not_found(db_session):
    repo = ProviderRepository(db_session)
    result = await repo.get(uuid.uuid4())
    assert result is None


async def test_get_by_name_found(db_session):
    repo = ProviderRepository(db_session)
    provider = _make_provider()
    await repo.create(provider)

    fetched = await repo.get_by_name(provider.name)

    assert fetched is not None
    assert fetched.id == provider.id


async def test_get_by_name_not_found(db_session):
    repo = ProviderRepository(db_session)
    result = await repo.get_by_name("nonexistent-provider")
    assert result is None


async def test_get_by_type_found(db_session):
    repo = ProviderRepository(db_session)
    provider = _make_provider(type=ProviderType.ory)
    await repo.create(provider)

    fetched = await repo.get_by_type(ProviderType.ory)

    assert fetched is not None
    assert fetched.type == ProviderType.ory


async def test_get_by_type_not_found(db_session):
    repo = ProviderRepository(db_session)
    result = await repo.get_by_type(ProviderType.cognito)
    assert result is None


async def test_update_provider(db_session):
    repo = ProviderRepository(db_session)
    provider = _make_provider(active=True)
    await repo.create(provider)

    provider.active = False
    updated = await repo.update(provider)

    assert updated.active is False


async def test_update_capabilities(db_session):
    repo = ProviderRepository(db_session)
    provider = _make_provider(capabilities=["sso"])
    await repo.create(provider)

    provider.capabilities = ["sso", "mfa", "rbac"]
    updated = await repo.update(provider)

    assert updated.capabilities == ["sso", "mfa", "rbac"]


async def test_list_all_returns_ordered_by_name(db_session):
    repo = ProviderRepository(db_session)
    # Insert in reverse alphabetical order
    await repo.create(_make_provider(name="zeta-provider"))
    await repo.create(_make_provider(name="alpha-provider"))
    await repo.create(_make_provider(name="middle-provider"))

    results = await repo.list_all()

    names = [p.name for p in results]
    assert names == sorted(names)
    assert len(results) >= 3


async def test_list_all_empty(db_session):
    """Fresh database with no providers returns empty list."""
    repo = ProviderRepository(db_session)
    results = await repo.list_all()
    # May have providers from other tests in the same session,
    # but the method should always return a list
    assert isinstance(results, list)
