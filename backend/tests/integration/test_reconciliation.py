"""Integration tests for ReconciliationService against real Postgres.

AC-3.4.3: Reconciliation integration tests with testcontainers Postgres + mocked Descope.
Tests cover:
- Drift detection: pre-populated canonical state vs different Descope state
- Resolution actions: correct upserts for tenants, permissions, roles, users
- Idempotency: second run produces zero changes
- Advisory lock acquisition
- Descope outage handling: abort without canonical modifications
- Status mapping: enabled->active, disabled->inactive, invited->provisioned
"""

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.dependencies.identity import _RECONCILIATION_LOCK_ID
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Permission, Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import User, UserStatus
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.permission import PermissionRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.reconciliation import ReconciliationService

# ── Fixtures ──────────────────────────────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def descope_provider(db_session):
    """Seed a Descope provider row (required for user reconciliation)."""
    provider = Provider(
        name=f"descope-test-{uuid.uuid4().hex[:8]}",
        type=ProviderType.descope,
        issuer_url="https://api.descope.com/test",
        base_url="https://api.descope.com",
    )
    db_session.add(provider)
    await db_session.flush()
    return provider


def _make_descope_client() -> AsyncMock:
    """Create a mock DescopeManagementClient with default empty responses."""
    client = AsyncMock()
    client.search_all_users = AsyncMock(return_value=[])
    client.list_roles = AsyncMock(return_value=[])
    client.list_permissions = AsyncMock(return_value=[])
    client.list_tenants = AsyncMock(return_value=[])
    return client


def _build_service(db_session, descope_client, publisher=None) -> ReconciliationService:
    """Build ReconciliationService wired to real repos and a mock Descope client."""

    async def acquire_lock():
        await db_session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _RECONCILIATION_LOCK_ID},
        )

    return ReconciliationService(
        session=db_session,
        acquire_lock=acquire_lock,
        descope_client=descope_client,
        user_repository=UserRepository(db_session),
        role_repository=RoleRepository(db_session),
        permission_repository=PermissionRepository(db_session),
        tenant_repository=TenantRepository(db_session),
        idp_link_repository=IdPLinkRepository(db_session),
        provider_repository=ProviderRepository(db_session),
        publisher=publisher,
    )


# ── Tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drift_detection_creates_new_entities(db_session, descope_provider):
    """Descope has entities that don't exist in canonical — reconciliation creates them."""
    suffix = uuid.uuid4().hex[:8]

    client = _make_descope_client()
    client.list_tenants.return_value = [
        {"name": f"tenant-{suffix}", "selfProvisioningDomains": ["example.com"]},
    ]
    client.list_permissions.return_value = [
        {"name": f"docs.write-{suffix}", "description": "Write documents"},
    ]
    client.list_roles.return_value = [
        {"name": f"editor-{suffix}", "description": "Can edit"},
    ]
    client.search_all_users.return_value = [
        {
            "userId": f"descope-{suffix}",
            "email": f"alice-{suffix}@test.com",
            "name": "Alice Wonderland",
            "status": "enabled",
        },
    ]

    svc = _build_service(db_session, client)
    result = await svc.run()

    assert result.is_ok()
    stats = result.ok["stats"]
    assert stats["tenants_created"] == 1
    assert stats["permissions_created"] == 1
    assert stats["roles_created"] == 1
    assert stats["users_created"] == 1
    assert stats["links_created"] == 1

    # Verify entities persisted in Postgres
    tenant_repo = TenantRepository(db_session)
    tenants = await tenant_repo.list_all()
    tenant_names = [t.name for t in tenants]
    assert f"tenant-{suffix}" in tenant_names

    perm_repo = PermissionRepository(db_session)
    perm = await perm_repo.get_by_name(f"docs.write-{suffix}")
    assert perm is not None
    assert perm.description == "Write documents"

    role_repo = RoleRepository(db_session)
    role = await role_repo.get_by_name(f"editor-{suffix}")
    assert role is not None
    assert role.description == "Can edit"

    user_repo = UserRepository(db_session)
    user = await user_repo.get_by_email(f"alice-{suffix}@test.com")
    assert user is not None
    assert user.given_name == "Alice"
    assert user.family_name == "Wonderland"
    assert user.status == UserStatus.active

    link_repo = IdPLinkRepository(db_session)
    link = await link_repo.get_by_provider_and_sub(provider_id=descope_provider.id, external_sub=f"descope-{suffix}")
    assert link is not None
    assert link.user_id == user.id


@pytest.mark.asyncio
async def test_drift_detection_updates_existing_entities(db_session, descope_provider):
    """Descope has different data for existing canonical entities — reconciliation updates."""
    suffix = uuid.uuid4().hex[:8]

    # Pre-populate canonical state
    tenant = Tenant(name=f"existing-tenant-{suffix}", domains=["old.com"], status=TenantStatus.active)
    db_session.add(tenant)
    await db_session.flush()

    perm = Permission(name=f"existing-perm-{suffix}", description="Old description")
    db_session.add(perm)
    await db_session.flush()

    role = Role(name=f"existing-role-{suffix}", description="Old role desc", tenant_id=None)
    db_session.add(role)
    await db_session.flush()

    user = User(
        email=f"existing-{suffix}@test.com",
        user_name=f"existing-{suffix}@test.com",
        given_name="Old",
        family_name="Name",
        status=UserStatus.inactive,
    )
    db_session.add(user)
    await db_session.flush()

    # Mock Descope returns updated data
    client = _make_descope_client()
    client.list_tenants.return_value = [
        {"name": f"existing-tenant-{suffix}", "selfProvisioningDomains": ["new.com"]},
    ]
    client.list_permissions.return_value = [
        {"name": f"existing-perm-{suffix}", "description": "New description"},
    ]
    client.list_roles.return_value = [
        {"name": f"existing-role-{suffix}", "description": "New role desc"},
    ]
    client.search_all_users.return_value = [
        {
            "userId": f"descope-existing-{suffix}",
            "email": f"existing-{suffix}@test.com",
            "givenName": "New",
            "familyName": "Person",
            "status": "enabled",
        },
    ]

    svc = _build_service(db_session, client)
    result = await svc.run()

    assert result.is_ok()
    stats = result.ok["stats"]
    assert stats["tenants_updated"] == 1
    assert stats["permissions_updated"] == 1
    assert stats["roles_updated"] == 1
    assert stats["users_updated"] == 1
    assert stats["links_created"] == 1

    # Verify updates persisted
    updated_tenant = await TenantRepository(db_session).get_by_name(f"existing-tenant-{suffix}")
    assert updated_tenant.domains == ["new.com"]

    updated_perm = await PermissionRepository(db_session).get_by_name(f"existing-perm-{suffix}")
    assert updated_perm.description == "New description"

    updated_role = await RoleRepository(db_session).get_by_name(f"existing-role-{suffix}")
    assert updated_role.description == "New role desc"

    updated_user = await UserRepository(db_session).get_by_email(f"existing-{suffix}@test.com")
    assert updated_user.given_name == "New"
    assert updated_user.family_name == "Person"
    assert updated_user.status == UserStatus.active


@pytest.mark.asyncio
async def test_idempotent_reconciliation(db_session, descope_provider):
    """Running reconciliation twice with same data produces zero changes on second run."""
    suffix = uuid.uuid4().hex[:8]

    descope_data = {
        "tenants": [{"name": f"idem-tenant-{suffix}", "selfProvisioningDomains": []}],
        "permissions": [{"name": f"idem-perm-{suffix}", "description": "test"}],
        "roles": [{"name": f"idem-role-{suffix}", "description": "test"}],
        "users": [
            {
                "userId": f"descope-idem-{suffix}",
                "email": f"idem-{suffix}@test.com",
                "givenName": "Idem",
                "familyName": "Potent",
                "status": "enabled",
            },
        ],
    }

    client = _make_descope_client()
    client.list_tenants.return_value = descope_data["tenants"]
    client.list_permissions.return_value = descope_data["permissions"]
    client.list_roles.return_value = descope_data["roles"]
    client.search_all_users.return_value = descope_data["users"]

    # First run — creates entities
    svc = _build_service(db_session, client)
    result1 = await svc.run()
    assert result1.is_ok()
    stats1 = result1.ok["stats"]
    assert stats1["tenants_created"] == 1
    assert stats1["permissions_created"] == 1
    assert stats1["roles_created"] == 1
    assert stats1["users_created"] == 1
    assert stats1["links_created"] == 1

    # Second run with same data — zero changes
    client2 = _make_descope_client()
    client2.list_tenants.return_value = descope_data["tenants"]
    client2.list_permissions.return_value = descope_data["permissions"]
    client2.list_roles.return_value = descope_data["roles"]
    client2.search_all_users.return_value = descope_data["users"]

    svc2 = _build_service(db_session, client2)
    result2 = await svc2.run()
    assert result2.is_ok()
    stats2 = result2.ok["stats"]
    for key, val in stats2.items():
        assert val == 0, f"Expected zero {key} on idempotent replay, got {val}"


@pytest.mark.asyncio
async def test_advisory_lock_acquisition(db_session, descope_provider):
    """Advisory lock is acquired successfully during reconciliation."""
    client = _make_descope_client()

    lock_acquired = False

    async def tracking_lock():
        nonlocal lock_acquired
        await db_session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _RECONCILIATION_LOCK_ID},
        )
        lock_acquired = True

    svc = ReconciliationService(
        session=db_session,
        acquire_lock=tracking_lock,
        descope_client=client,
        user_repository=UserRepository(db_session),
        role_repository=RoleRepository(db_session),
        permission_repository=PermissionRepository(db_session),
        tenant_repository=TenantRepository(db_session),
        idp_link_repository=IdPLinkRepository(db_session),
        provider_repository=ProviderRepository(db_session),
    )

    result = await svc.run()
    assert result.is_ok()
    assert lock_acquired, "Advisory lock should have been acquired"


@pytest.mark.asyncio
async def test_descope_outage_aborts_without_changes(db_session, descope_provider):
    """When Descope API fails, reconciliation aborts without modifying canonical state."""
    suffix = uuid.uuid4().hex[:8]

    # Pre-populate a tenant so we can verify it's unchanged after abort
    tenant = Tenant(name=f"pre-outage-{suffix}", domains=["keep.com"], status=TenantStatus.active)
    db_session.add(tenant)
    await db_session.flush()

    client = _make_descope_client()
    client.search_all_users.side_effect = Exception("Descope API timeout")

    svc = _build_service(db_session, client)
    result = await svc.run()

    assert result.is_error()
    assert "unavailable" in result.error.message.lower()

    # Verify pre-existing data is untouched
    existing = await TenantRepository(db_session).get_by_name(f"pre-outage-{suffix}")
    assert existing is not None
    assert existing.domains == ["keep.com"]


@pytest.mark.asyncio
async def test_descope_status_mapping(db_session, descope_provider):
    """Descope status values map correctly: enabled->active, disabled->inactive, invited->provisioned."""
    suffix = uuid.uuid4().hex[:8]

    client = _make_descope_client()
    client.search_all_users.return_value = [
        {
            "userId": f"enabled-{suffix}",
            "email": f"enabled-{suffix}@test.com",
            "givenName": "Enabled",
            "familyName": "User",
            "status": "enabled",
        },
        {
            "userId": f"disabled-{suffix}",
            "email": f"disabled-{suffix}@test.com",
            "givenName": "Disabled",
            "familyName": "User",
            "status": "disabled",
        },
        {
            "userId": f"invited-{suffix}",
            "email": f"invited-{suffix}@test.com",
            "givenName": "Invited",
            "familyName": "User",
            "status": "invited",
        },
        {
            "userId": f"unknown-{suffix}",
            "email": f"unknown-{suffix}@test.com",
            "givenName": "Unknown",
            "familyName": "User",
            "status": "some_unknown_status",
        },
    ]

    svc = _build_service(db_session, client)
    result = await svc.run()
    assert result.is_ok()

    user_repo = UserRepository(db_session)

    enabled = await user_repo.get_by_email(f"enabled-{suffix}@test.com")
    assert enabled.status == UserStatus.active

    disabled = await user_repo.get_by_email(f"disabled-{suffix}@test.com")
    assert disabled.status == UserStatus.inactive

    invited = await user_repo.get_by_email(f"invited-{suffix}@test.com")
    assert invited.status == UserStatus.provisioned

    unknown = await user_repo.get_by_email(f"unknown-{suffix}@test.com")
    assert unknown.status == UserStatus.inactive  # Unknown defaults to inactive


@pytest.mark.asyncio
async def test_empty_descope_state(db_session, descope_provider):
    """Reconciliation with no entities in Descope produces zero changes."""
    client = _make_descope_client()

    svc = _build_service(db_session, client)
    result = await svc.run()

    assert result.is_ok()
    stats = result.ok["stats"]
    assert sum(stats.values()) == 0


@pytest.mark.asyncio
async def test_user_name_splitting(db_session, descope_provider):
    """User with only 'name' field gets it split into given_name/family_name."""
    suffix = uuid.uuid4().hex[:8]

    client = _make_descope_client()
    client.search_all_users.return_value = [
        {
            "userId": f"split-{suffix}",
            "email": f"split-{suffix}@test.com",
            "name": "Jean-Pierre de la Fontaine",
            "status": "enabled",
        },
    ]

    svc = _build_service(db_session, client)
    result = await svc.run()
    assert result.is_ok()

    user = await UserRepository(db_session).get_by_email(f"split-{suffix}@test.com")
    assert user is not None
    assert user.given_name == "Jean-Pierre"
    assert user.family_name == "de la Fontaine"


@pytest.mark.asyncio
async def test_skips_users_without_email(db_session, descope_provider):
    """Users missing email or userId are silently skipped."""
    suffix = uuid.uuid4().hex[:8]

    client = _make_descope_client()
    client.search_all_users.return_value = [
        {"userId": f"no-email-{suffix}", "status": "enabled"},
        {"email": f"no-uid-{suffix}@test.com", "status": "enabled"},
        {
            "userId": f"valid-{suffix}",
            "email": f"valid-{suffix}@test.com",
            "givenName": "Valid",
            "familyName": "User",
            "status": "enabled",
        },
    ]

    svc = _build_service(db_session, client)
    result = await svc.run()
    assert result.is_ok()
    assert result.ok["stats"]["users_created"] == 1

    user = await UserRepository(db_session).get_by_email(f"valid-{suffix}@test.com")
    assert user is not None


@pytest.mark.asyncio
async def test_lock_failure_returns_error(db_session, descope_provider):
    """When advisory lock acquisition fails, reconciliation returns error without changes."""
    suffix = uuid.uuid4().hex[:8]
    client = _make_descope_client()
    client.list_tenants.return_value = [{"name": f"should-not-exist-{suffix}"}]

    async def failing_lock():
        raise RuntimeError("Lock contention")

    svc = ReconciliationService(
        session=db_session,
        acquire_lock=failing_lock,
        descope_client=client,
        user_repository=UserRepository(db_session),
        role_repository=RoleRepository(db_session),
        permission_repository=PermissionRepository(db_session),
        tenant_repository=TenantRepository(db_session),
        idp_link_repository=IdPLinkRepository(db_session),
        provider_repository=ProviderRepository(db_session),
    )

    result = await svc.run()
    assert result.is_error()
    assert "lock" in result.error.message.lower()

    # Verify nothing was created
    existing = await TenantRepository(db_session).get_by_name(f"should-not-exist-{suffix}")
    assert existing is None


@pytest.mark.asyncio
async def test_publisher_called_on_changes(db_session, descope_provider):
    """CacheInvalidationPublisher.publish_batch is called when reconciliation makes changes."""
    suffix = uuid.uuid4().hex[:8]

    publisher = AsyncMock()
    publisher.publish_batch = AsyncMock()

    client = _make_descope_client()
    client.list_tenants.return_value = [
        {"name": f"pub-tenant-{suffix}", "selfProvisioningDomains": []},
    ]

    svc = _build_service(db_session, client, publisher=publisher)
    result = await svc.run()

    assert result.is_ok()
    publisher.publish_batch.assert_awaited_once()
    call_kwargs = publisher.publish_batch.call_args
    assert call_kwargs.kwargs["operation"] == "reconcile"


@pytest.mark.asyncio
async def test_publisher_not_called_when_no_changes(db_session, descope_provider):
    """CacheInvalidationPublisher is NOT called when reconciliation makes zero changes."""
    publisher = AsyncMock()
    publisher.publish_batch = AsyncMock()

    client = _make_descope_client()
    svc = _build_service(db_session, client, publisher=publisher)
    result = await svc.run()

    assert result.is_ok()
    publisher.publish_batch.assert_not_awaited()
