"""Integration tests for IdentityProvisioningService (JIT provisioning) against Postgres."""

import pytest

from app.models.identity.provider import Provider, ProviderType
from app.models.identity.user import User, UserStatus
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
        email_verified=True,
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
    assert tenant is not None
    # The default role is scoped to the default tenant (not a global role).
    role = await RoleRepository(db_session).get_by_name(DEFAULT_ROLE_NAME, tenant_id=tenant.id)
    assert role is not None
    assert role.tenant_id == tenant.id

    assignments = await UserTenantRoleRepository(db_session).list_by_user(user_id)
    assert len(assignments) == 1
    assert assignments[0].tenant_id == tenant.id
    assert assignments[0].role_id == role.id


@pytest.mark.asyncio
async def test_provision_is_idempotent(db_session):
    await _seed_ory_provider(db_session)
    svc = _service(db_session)
    first = await svc.provision(provider_name="ory", sub="ory-sub-2", email="dup@example.com", email_verified=True)
    second = await _service(db_session).provision(
        provider_name="ory", sub="ory-sub-2", email="dup@example.com", email_verified=True
    )
    assert first.is_ok() and second.is_ok()
    assert first.ok == second.ok

    links = await IdPLinkRepository(db_session).get_by_user(first.ok)
    assert len([link for link in links if link.external_sub == "ory-sub-2"]) == 1
    assignments = await UserTenantRoleRepository(db_session).list_by_user(first.ok)
    assert len(assignments) == 1


@pytest.mark.asyncio
async def test_unverified_email_is_rejected_and_creates_nothing(db_session):
    """Account-takeover guard: an unverified email must never provision or link."""
    await _seed_ory_provider(db_session)
    result = await _service(db_session).provision(
        provider_name="ory", sub="ory-sub-unv", email="unverified@example.com", email_verified=False
    )
    assert result.is_error()
    assert await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", "ory-sub-unv") is None
    assert await UserRepository(db_session).get_by_email("unverified@example.com") is None


@pytest.mark.asyncio
async def test_unverified_email_cannot_hijack_existing_user(db_session):
    """Core takeover scenario: an attacker asserting a victim's email (unverified)
    must NOT be linked to the victim's existing canonical user."""
    await _seed_ory_provider(db_session)
    victim = User(email="victim@corp.com", user_name="victim@corp.com", status=UserStatus.active)
    db_session.add(victim)
    await db_session.flush()

    result = await _service(db_session).provision(
        provider_name="ory", sub="ory|attacker", email="victim@corp.com", email_verified=False
    )
    assert result.is_error()
    # No Ory link was created against the victim's user.
    assert await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", "ory|attacker") is None
    assert await IdPLinkRepository(db_session).get_by_user(victim.id) == []


@pytest.mark.asyncio
async def test_verified_email_links_new_provider_to_existing_user(db_session):
    """A VERIFIED email may legitimately link a new provider identity to the
    canonical user that already owns that email (cross-provider account linking)."""
    await _seed_ory_provider(db_session)
    existing = User(email="both@corp.com", user_name="both@corp.com", status=UserStatus.active)
    db_session.add(existing)
    await db_session.flush()

    result = await _service(db_session).provision(
        provider_name="ory", sub="ory|both", email="both@corp.com", email_verified=True
    )
    assert result.is_ok()
    assert result.ok == existing.id
    link = await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", "ory|both")
    assert link is not None and link.user_id == existing.id


@pytest.mark.asyncio
async def test_unknown_provider_returns_not_found(db_session):
    result = await _service(db_session).provision(
        provider_name="ory", sub="s", email="x@example.com", email_verified=True
    )
    assert result.is_error()


@pytest.mark.asyncio
async def test_distinct_subjects_share_default_tenant_and_role(db_session):
    await _seed_ory_provider(db_session)
    svc = _service(db_session)
    a = await svc.provision(provider_name="ory", sub="sub-a", email="a@example.com", email_verified=True)
    b = await _service(db_session).provision(
        provider_name="ory", sub="sub-b", email="b@example.com", email_verified=True
    )
    assert a.is_ok() and b.is_ok()
    assert a.ok != b.ok

    tenant = await TenantRepository(db_session).get_by_name(DEFAULT_TENANT_NAME)
    role = await RoleRepository(db_session).get_by_name(DEFAULT_ROLE_NAME, tenant_id=tenant.id)
    a_assign = await UserTenantRoleRepository(db_session).list_by_user(a.ok)
    b_assign = await UserTenantRoleRepository(db_session).list_by_user(b.ok)
    assert a_assign[0].tenant_id == tenant.id == b_assign[0].tenant_id
    assert a_assign[0].role_id == role.id == b_assign[0].role_id
