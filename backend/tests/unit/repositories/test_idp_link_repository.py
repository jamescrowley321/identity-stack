"""Unit tests for IdPLinkRepository — data access layer tests against real Postgres.

Tests cover:
- create: happy path, duplicate user+provider → RepositoryConflictError,
          duplicate provider+external_sub → RepositoryConflictError
- get: found, not found
- delete: found → True, not found → False
- get_by_user: returns all links for user, empty when none
- get_by_provider_and_sub: found, not found
"""

import uuid

import pytest

from app.models.identity.provider import Provider, ProviderType
from app.models.identity.user import IdPLink, User, UserStatus
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.user import RepositoryConflictError

pytestmark = pytest.mark.asyncio


def _make_user(**overrides) -> User:
    defaults = {
        "id": uuid.uuid4(),
        "email": f"user-{uuid.uuid4().hex[:8]}@test.com",
        "user_name": "testuser",
        "given_name": "Test",
        "family_name": "User",
        "status": UserStatus.active,
    }
    defaults.update(overrides)
    return User(**defaults)


def _make_provider(**overrides) -> Provider:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"provider-{uuid.uuid4().hex[:8]}",
        "type": ProviderType.descope,
    }
    defaults.update(overrides)
    return Provider(**defaults)


def _make_idp_link(user_id: uuid.UUID, provider_id: uuid.UUID, **overrides) -> IdPLink:
    defaults = {
        "user_id": user_id,
        "provider_id": provider_id,
        "external_sub": f"ext-{uuid.uuid4().hex[:8]}",
        "external_email": "ext@example.com",
    }
    defaults.update(overrides)
    return IdPLink(**defaults)


async def _seed_user_and_provider(db_session) -> tuple[User, Provider]:
    """Insert a user and provider to satisfy FK constraints."""
    user = _make_user()
    provider = _make_provider()
    db_session.add(user)
    db_session.add(provider)
    await db_session.flush()
    return user, provider


async def test_create_idp_link(db_session):
    user, provider = await _seed_user_and_provider(db_session)
    repo = IdPLinkRepository(db_session)
    link = _make_idp_link(user.id, provider.id)

    created = await repo.create(link)

    assert created.id == link.id
    assert created.user_id == user.id
    assert created.provider_id == provider.id


async def test_create_duplicate_user_provider(db_session):
    """Unique constraint: same user + same provider → RepositoryConflictError."""
    user, provider = await _seed_user_and_provider(db_session)
    repo = IdPLinkRepository(db_session)

    await repo.create(_make_idp_link(user.id, provider.id, external_sub="sub-1"))
    with pytest.raises(RepositoryConflictError):
        await repo.create(_make_idp_link(user.id, provider.id, external_sub="sub-2"))


async def test_create_duplicate_provider_external_sub(db_session):
    """Unique constraint: same provider + same external_sub → RepositoryConflictError."""
    user1 = _make_user()
    user2 = _make_user()
    provider = _make_provider()
    db_session.add_all([user1, user2, provider])
    await db_session.flush()
    repo = IdPLinkRepository(db_session)
    ext_sub = "shared-ext-sub"

    await repo.create(_make_idp_link(user1.id, provider.id, external_sub=ext_sub))
    with pytest.raises(RepositoryConflictError):
        await repo.create(_make_idp_link(user2.id, provider.id, external_sub=ext_sub))


async def test_get_found(db_session):
    user, provider = await _seed_user_and_provider(db_session)
    repo = IdPLinkRepository(db_session)
    link = _make_idp_link(user.id, provider.id)
    await repo.create(link)

    fetched = await repo.get(link.id)

    assert fetched is not None
    assert fetched.id == link.id


async def test_get_not_found(db_session):
    repo = IdPLinkRepository(db_session)
    result = await repo.get(uuid.uuid4())
    assert result is None


async def test_delete_found(db_session):
    user, provider = await _seed_user_and_provider(db_session)
    repo = IdPLinkRepository(db_session)
    link = _make_idp_link(user.id, provider.id)
    await repo.create(link)

    deleted = await repo.delete(link.id)

    assert deleted is True
    assert await repo.get(link.id) is None


async def test_delete_not_found(db_session):
    repo = IdPLinkRepository(db_session)
    deleted = await repo.delete(uuid.uuid4())
    assert deleted is False


async def test_get_by_user(db_session):
    user = _make_user()
    p1 = _make_provider()
    p2 = _make_provider()
    db_session.add_all([user, p1, p2])
    await db_session.flush()
    repo = IdPLinkRepository(db_session)

    await repo.create(_make_idp_link(user.id, p1.id))
    await repo.create(_make_idp_link(user.id, p2.id))

    links = await repo.get_by_user(user.id)
    assert len(links) == 2
    assert all(link.user_id == user.id for link in links)


async def test_get_by_user_empty(db_session):
    repo = IdPLinkRepository(db_session)
    links = await repo.get_by_user(uuid.uuid4())
    assert links == []


async def test_get_by_provider_and_sub(db_session):
    user, provider = await _seed_user_and_provider(db_session)
    repo = IdPLinkRepository(db_session)
    ext_sub = "unique-ext-sub"
    link = _make_idp_link(user.id, provider.id, external_sub=ext_sub)
    await repo.create(link)

    fetched = await repo.get_by_provider_and_sub(provider.id, ext_sub)

    assert fetched is not None
    assert fetched.external_sub == ext_sub


async def test_get_by_provider_and_sub_not_found(db_session):
    repo = IdPLinkRepository(db_session)
    result = await repo.get_by_provider_and_sub(uuid.uuid4(), "no-such-sub")
    assert result is None
