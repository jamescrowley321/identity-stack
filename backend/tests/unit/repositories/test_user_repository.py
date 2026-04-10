"""Unit tests for UserRepository — data access layer tests against real Postgres.

Tests cover:
- create: happy path, duplicate email → RepositoryConflictError
- get: found, not found
- get_by_email: found, not found
- update: happy path
- search: tenant-scoped via UserTenantRole JOIN, email/name/status filters
- exists_in_tenant: true when assigned, false when not
"""

import uuid

import pytest

from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.models.identity.user import User, UserStatus
from app.repositories.user import RepositoryConflictError, UserRepository

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


def _make_tenant(**overrides) -> Tenant:
    defaults = {"id": uuid.uuid4(), "name": f"tenant-{uuid.uuid4().hex[:8]}"}
    defaults.update(overrides)
    return Tenant(**defaults)


def _make_role(tenant_id=None, **overrides) -> Role:
    defaults = {
        "id": uuid.uuid4(),
        "name": f"role-{uuid.uuid4().hex[:8]}",
        "tenant_id": tenant_id,
    }
    defaults.update(overrides)
    return Role(**defaults)


async def test_create_user(db_session):
    repo = UserRepository(db_session)
    user = _make_user()
    created = await repo.create(user)
    assert created.id == user.id
    assert created.email == user.email
    assert created.created_at is not None


async def test_create_user_duplicate_email(db_session):
    repo = UserRepository(db_session)
    email = f"dup-{uuid.uuid4().hex[:8]}@test.com"
    await repo.create(_make_user(email=email))
    with pytest.raises(RepositoryConflictError):
        await repo.create(_make_user(email=email))


async def test_get_user(db_session):
    repo = UserRepository(db_session)
    user = _make_user()
    await repo.create(user)
    fetched = await repo.get(user.id)
    assert fetched is not None
    assert fetched.id == user.id
    assert fetched.email == user.email


async def test_get_user_not_found(db_session):
    repo = UserRepository(db_session)
    result = await repo.get(uuid.uuid4())
    assert result is None


async def test_get_by_email(db_session):
    repo = UserRepository(db_session)
    user = _make_user()
    await repo.create(user)
    fetched = await repo.get_by_email(user.email)
    assert fetched is not None
    assert fetched.id == user.id


async def test_get_by_email_not_found(db_session):
    repo = UserRepository(db_session)
    result = await repo.get_by_email("nonexistent@test.com")
    assert result is None


async def test_update_user(db_session):
    repo = UserRepository(db_session)
    user = _make_user()
    await repo.create(user)
    user.given_name = "Updated"
    updated = await repo.update(user)
    assert updated.given_name == "Updated"


async def test_search_tenant_scoped(db_session):
    """Users are only returned when they have a role assignment in the tenant."""
    repo = UserRepository(db_session)

    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()
    role = _make_role(tenant_id=tenant.id)
    db_session.add(role)
    await db_session.flush()

    user_in = _make_user()
    user_out = _make_user()
    await repo.create(user_in)
    await repo.create(user_out)

    # Assign user_in to tenant
    assignment = UserTenantRole(user_id=user_in.id, tenant_id=tenant.id, role_id=role.id)
    db_session.add(assignment)
    await db_session.flush()

    results = await repo.search(tenant_id=tenant.id)
    result_ids = [u.id for u in results]
    assert user_in.id in result_ids
    assert user_out.id not in result_ids


async def test_search_email_filter(db_session):
    repo = UserRepository(db_session)

    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()
    role = _make_role(tenant_id=tenant.id)
    db_session.add(role)
    await db_session.flush()

    unique = uuid.uuid4().hex[:8]
    user = _make_user(email=f"findme-{unique}@test.com")
    await repo.create(user)
    db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
    await db_session.flush()

    results = await repo.search(tenant_id=tenant.id, email=f"findme-{unique}")
    assert len(results) == 1
    assert results[0].id == user.id


async def test_search_name_filter(db_session):
    repo = UserRepository(db_session)

    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()
    role = _make_role(tenant_id=tenant.id)
    db_session.add(role)
    await db_session.flush()

    unique = uuid.uuid4().hex[:8]
    user = _make_user(given_name=f"Unique{unique}")
    await repo.create(user)
    db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
    await db_session.flush()

    results = await repo.search(tenant_id=tenant.id, name=f"Unique{unique}")
    assert len(results) == 1
    assert results[0].id == user.id


async def test_search_status_filter(db_session):
    repo = UserRepository(db_session)

    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()
    role = _make_role(tenant_id=tenant.id)
    db_session.add(role)
    await db_session.flush()

    active_user = _make_user(status=UserStatus.active)
    inactive_user = _make_user(status=UserStatus.inactive)
    await repo.create(active_user)
    await repo.create(inactive_user)
    db_session.add(UserTenantRole(user_id=active_user.id, tenant_id=tenant.id, role_id=role.id))
    db_session.add(UserTenantRole(user_id=inactive_user.id, tenant_id=tenant.id, role_id=role.id))
    await db_session.flush()

    results = await repo.search(tenant_id=tenant.id, status=UserStatus.inactive)
    result_ids = [u.id for u in results]
    assert inactive_user.id in result_ids
    assert active_user.id not in result_ids


async def test_exists_in_tenant_true(db_session):
    repo = UserRepository(db_session)

    tenant = _make_tenant()
    db_session.add(tenant)
    await db_session.flush()
    role = _make_role(tenant_id=tenant.id)
    db_session.add(role)
    await db_session.flush()

    user = _make_user()
    await repo.create(user)
    db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
    await db_session.flush()

    assert await repo.exists_in_tenant(user.id, tenant.id) is True


async def test_exists_in_tenant_false(db_session):
    repo = UserRepository(db_session)
    user = _make_user()
    await repo.create(user)
    assert await repo.exists_in_tenant(user.id, uuid.uuid4()) is False


async def test_deactivate_flow(db_session):
    """Simulate deactivation: create user, update status to inactive, verify."""
    repo = UserRepository(db_session)
    user = _make_user(status=UserStatus.active)
    await repo.create(user)

    user.status = UserStatus.inactive
    await repo.update(user)

    fetched = await repo.get(user.id)
    assert fetched is not None
    assert fetched.status == UserStatus.inactive
