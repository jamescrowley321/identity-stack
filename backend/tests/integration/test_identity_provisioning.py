"""Integration tests for IdentityProvisioningService (JIT provisioning) against Postgres."""

import pytest

from app.models.identity.provider import Provider, ProviderType
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.identity_provisioning import (
    DEFAULT_ROLE_NAME,
    DEFAULT_TENANT_NAME,
    IdentityProvisioningService,
)


def _service(session) -> IdentityProvisioningService:
    return IdentityProvisioningService(
        session=session,
        provider_repository=ProviderRepository(session),
        idp_link_repository=IdPLinkRepository(session),
        user_repository=UserRepository(session),
        tenant_repository=TenantRepository(session),
        role_repository=RoleRepository(session),
        assignment_repository=UserTenantRoleRepository(session),
    )


async def _seed_ory_provider(session) -> Provider:
    provider = Provider(
        name="ory",
        type=ProviderType.ory,
        issuer_url="https://example.projects.oryapis.com",
        capabilities=["sso"],
    )
    session.add(provider)
    await session.flush()
    return provider


@pytest.mark.asyncio
async def test_provision_creates_user_link_and_default_role(db_session):
    await _seed_ory_provider(db_session)
    result = await _service(db_session).provision(
        provider_name="ory",
        sub="ory-sub-1",
        email="new@example.com",
        given_name="New",
        family_name="User",
    )
    assert result.is_ok()
    user_id = result.ok

    link = await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", "ory-sub-1")
    assert link is not None
    assert link.user_id == user_id
    assert link.external_email == "new@example.com"

    user = await UserRepository(db_session).get_by_email("new@example.com")
    assert user is not None
    assert user.id == user_id
    assert user.given_name == "New"
    assert user.family_name == "User"

    tenant = await TenantRepository(db_session).get_by_name(DEFAULT_TENANT_NAME)
    role = await RoleRepository(db_session).get_by_name(DEFAULT_ROLE_NAME, tenant_id=None)
    assert tenant is not None and role is not None

    assignments = await UserTenantRoleRepository(db_session).list_by_user(user_id)
    assert len(assignments) == 1
    assert assignments[0].tenant_id == tenant.id
    assert assignments[0].role_id == role.id


@pytest.mark.asyncio
async def test_provision_is_idempotent(db_session):
    await _seed_ory_provider(db_session)
    svc = _service(db_session)
    first = await svc.provision(provider_name="ory", sub="ory-sub-2", email="dup@example.com")
    second = await _service(db_session).provision(provider_name="ory", sub="ory-sub-2", email="dup@example.com")
    assert first.is_ok() and second.is_ok()
    assert first.ok == second.ok

    # Exactly one link and one assignment — no duplicates.
    links = await IdPLinkRepository(db_session).get_by_user(first.ok)
    assert len([link for link in links if link.external_sub == "ory-sub-2"]) == 1
    assignments = await UserTenantRoleRepository(db_session).list_by_user(first.ok)
    assert len(assignments) == 1


@pytest.mark.asyncio
async def test_missing_email_returns_validation_error(db_session):
    await _seed_ory_provider(db_session)
    result = await _service(db_session).provision(provider_name="ory", sub="ory-sub-3", email="")
    assert result.is_error()
    # No user/link created.
    link = await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", "ory-sub-3")
    assert link is None


@pytest.mark.asyncio
async def test_unknown_provider_returns_not_found(db_session):
    # No provider seeded.
    result = await _service(db_session).provision(provider_name="ory", sub="s", email="x@example.com")
    assert result.is_error()


@pytest.mark.asyncio
async def test_distinct_subjects_share_default_tenant_and_role(db_session):
    await _seed_ory_provider(db_session)
    svc = _service(db_session)
    a = await svc.provision(provider_name="ory", sub="sub-a", email="a@example.com")
    b = await _service(db_session).provision(provider_name="ory", sub="sub-b", email="b@example.com")
    assert a.is_ok() and b.is_ok()
    assert a.ok != b.ok

    # Only one default tenant and one default role exist (shared).
    tenant = await TenantRepository(db_session).get_by_name(DEFAULT_TENANT_NAME)
    role = await RoleRepository(db_session).get_by_name(DEFAULT_ROLE_NAME, tenant_id=None)
    a_assign = await UserTenantRoleRepository(db_session).list_by_user(a.ok)
    b_assign = await UserTenantRoleRepository(db_session).list_by_user(b.ok)
    assert a_assign[0].tenant_id == tenant.id == b_assign[0].tenant_id
    assert a_assign[0].role_id == role.id == b_assign[0].role_id
