"""Unit tests for PostgresIdentityService (Stories 2.1 + 2.2).

Covers:
- create_user: happy path, duplicate email (Conflict), sync failure (logged, still Ok)
- get_user: found (tenant-scoped), not found
- update_user: happy path (tenant-scoped), not found, duplicate email, sync failure
- deactivate_user: happy path (tenant-scoped), not found, sync failure, IntegrityError
- search_users: tenant-scoped, with query, empty results, status filter, wildcard escape
- Role CRUD: create, get, update, delete + duplicate constraints + sync failure
- Permission CRUD: create, get, update, delete + duplicate constraints
- Permission mapping: map/unmap + validation + duplicate
- Tenant CRUD: create, get, update, delete + duplicate constraints + sync failure
- User-tenant-role assignment: assign, remove, get_tenant_users_with_roles
- OTel spans created for each operation with correct attributes
- Serializer tests for _role_to_dict, _permission_to_dict, _tenant_to_dict
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from expression import Error
from sqlalchemy.exc import IntegrityError

from app.errors.identity import Conflict, NotFound
from app.models.identity.role import Permission, Role, RolePermission
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import User, UserStatus
from app.services.adapters.base import SyncError
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.identity_impl import PostgresIdentityService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def noop_adapter():
    return NoOpSyncAdapter()


@pytest.fixture
def mock_session():
    """AsyncSession mock with standard execute/flush/rollback."""
    session = AsyncMock()
    return session


@pytest.fixture
def service(mock_session, noop_adapter):
    return PostgresIdentityService(session=mock_session, adapter=noop_adapter)


@pytest.fixture
def tenant_id():
    return uuid.uuid4()


def _make_user(
    *,
    user_id: uuid.UUID | None = None,
    email: str = "alice@example.com",
    user_name: str = "alice",
    given_name: str = "Alice",
    family_name: str = "Smith",
    status: UserStatus = UserStatus.active,
) -> User:
    """Create a User model instance for testing."""
    return User(
        id=user_id or uuid.uuid4(),
        email=email,
        user_name=user_name,
        given_name=given_name,
        family_name=family_name,
        status=status,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# create_user
# ---------------------------------------------------------------------------


class TestCreateUser:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session, tenant_id):
        mock_session.flush = AsyncMock()
        result = await service.create_user(
            tenant_id=tenant_id,
            email="alice@example.com",
            user_name="alice",
            given_name="Alice",
            family_name="Smith",
        )
        assert result.is_ok()
        user_dict = result.ok
        assert user_dict["email"] == "alice@example.com"
        assert user_dict["user_name"] == "alice"
        assert user_dict["status"] == "active"
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()

    @pytest.mark.anyio
    async def test_duplicate_email_returns_conflict(self, service, mock_session, tenant_id):
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()
        result = await service.create_user(
            tenant_id=tenant_id,
            email="dup@example.com",
            user_name="dup",
        )
        assert result.is_error()
        err = result.error
        assert isinstance(err, Conflict)
        assert "dup@example.com" in err.message

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session, tenant_id):
        """D7: sync failure → log, never rollback, still return Ok."""
        adapter = AsyncMock()
        adapter.sync_user = AsyncMock(return_value=Error(SyncError(message="timeout", operation="sync_user")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.create_user(
                tenant_id=tenant_id,
                email="sync-fail@example.com",
                user_name="syncfail",
            )
            assert result.is_ok()
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_sync_failure_logs_payload(self, mock_session, tenant_id):
        """AC-2.1.1: Sync failure log includes operation, payload, error, timestamp."""
        adapter = AsyncMock()
        adapter.sync_user = AsyncMock(return_value=Error(SyncError(message="timeout", operation="sync_user")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            await svc.create_user(
                tenant_id=tenant_id,
                email="payload@example.com",
                user_name="payload",
            )
            call_args = mock_logger.warning.call_args
            log_msg = call_args[0][0]
            # Verify log format includes payload placeholder
            assert "payload" in log_msg

    @pytest.mark.anyio
    async def test_includes_all_fields(self, service, mock_session, tenant_id):
        mock_session.flush = AsyncMock()
        result = await service.create_user(
            tenant_id=tenant_id,
            email="full@example.com",
            user_name="fulluser",
            given_name="Full",
            family_name="User",
        )
        d = result.ok
        assert "id" in d
        assert "created_at" in d
        assert "updated_at" in d
        assert d["given_name"] == "Full"
        assert d["family_name"] == "User"


# ---------------------------------------------------------------------------
# get_user
# ---------------------------------------------------------------------------


class TestGetUser:
    @pytest.mark.anyio
    async def test_found(self, service, mock_session, tenant_id):
        user = _make_user()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_user(tenant_id=tenant_id, user_id=user.id)
        assert result.is_ok()
        assert result.ok["email"] == user.email
        assert result.ok["id"] == str(user.id)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session, tenant_id):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        uid = uuid.uuid4()
        result = await service.get_user(tenant_id=tenant_id, user_id=uid)
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert str(uid) in result.error.message
        assert str(tenant_id) in result.error.message


# ---------------------------------------------------------------------------
# update_user
# ---------------------------------------------------------------------------


class TestUpdateUser:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session, tenant_id):
        user = _make_user()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        result = await service.update_user(
            tenant_id=tenant_id,
            user_id=user.id,
            email="newemail@example.com",
        )
        assert result.is_ok()
        assert user.email == "newemail@example.com"

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session, tenant_id):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.update_user(tenant_id=tenant_id, user_id=uuid.uuid4(), email="x@y.com")
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_duplicate_email(self, service, mock_session, tenant_id):
        user = _make_user()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.update_user(tenant_id=tenant_id, user_id=user.id, email="taken@example.com")
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        # Error message should show the actual email on the user model, not the param
        assert "taken@example.com" in result.error.message

    @pytest.mark.anyio
    async def test_integrity_error_message_uses_model_email(self, service, mock_session, tenant_id):
        """When only user_name is updated but IntegrityError occurs, message uses user.email not None."""
        user = _make_user(email="real@example.com")
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.update_user(tenant_id=tenant_id, user_id=user.id, user_name="newname")
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "real@example.com" in result.error.message

    @pytest.mark.anyio
    async def test_partial_update_only_changes_given_fields(self, service, mock_session, tenant_id):
        user = _make_user(given_name="Original", family_name="Name")
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_user(tenant_id=tenant_id, user_id=user.id, given_name="Updated")
        assert user.given_name == "Updated"
        assert user.family_name == "Name"  # unchanged

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session, tenant_id):
        adapter = AsyncMock()
        adapter.sync_user = AsyncMock(return_value=Error(SyncError(message="timeout", operation="sync_user")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        user = _make_user()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.update_user(tenant_id=tenant_id, user_id=user.id, email="new@test.com")
            assert result.is_ok()
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_updates_timestamp(self, service, mock_session, tenant_id):
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        user = _make_user()
        user.updated_at = old_time
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_user(tenant_id=tenant_id, user_id=user.id, given_name="Changed")
        assert user.updated_at > old_time


# ---------------------------------------------------------------------------
# deactivate_user
# ---------------------------------------------------------------------------


class TestDeactivateUser:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session, tenant_id):
        user = _make_user(status=UserStatus.active)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        result = await service.deactivate_user(tenant_id=tenant_id, user_id=user.id)
        assert result.is_ok()
        assert result.ok["status"] == "inactive"
        assert user.status == UserStatus.inactive

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session, tenant_id):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.deactivate_user(tenant_id=tenant_id, user_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session, tenant_id):
        adapter = AsyncMock()
        adapter.sync_user = AsyncMock(return_value=Error(SyncError(message="timeout", operation="sync_user")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        user = _make_user()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.deactivate_user(tenant_id=tenant_id, user_id=user.id)
            assert result.is_ok()
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_updates_timestamp(self, service, mock_session, tenant_id):
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        user = _make_user()
        user.updated_at = old_time
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.deactivate_user(tenant_id=tenant_id, user_id=user.id)
        assert user.updated_at > old_time

    @pytest.mark.anyio
    async def test_integrity_error_returns_conflict(self, service, mock_session, tenant_id):
        user = _make_user()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("constraint", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.deactivate_user(tenant_id=tenant_id, user_id=user.id)
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "deactivation" in result.error.message


# ---------------------------------------------------------------------------
# search_users
# ---------------------------------------------------------------------------


class TestSearchUsers:
    @pytest.mark.anyio
    async def test_returns_users_for_tenant(self, service, mock_session, tenant_id):
        users = [_make_user(email="a@test.com"), _make_user(email="b@test.com")]
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = users
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.search_users(tenant_id=tenant_id)
        assert result.is_ok()
        assert len(result.ok) == 2
        assert result.ok[0]["email"] == "a@test.com"
        assert result.ok[1]["email"] == "b@test.com"

    @pytest.mark.anyio
    async def test_empty_results(self, service, mock_session, tenant_id):
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.search_users(tenant_id=tenant_id)
        assert result.is_ok()
        assert result.ok == []

    @pytest.mark.anyio
    async def test_with_query_param(self, service, mock_session, tenant_id):
        users = [_make_user(email="match@test.com")]
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = users
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.search_users(tenant_id=tenant_id, query="match")
        assert result.is_ok()
        assert len(result.ok) == 1

    @pytest.mark.anyio
    async def test_with_status_filter(self, service, mock_session, tenant_id):
        users = [_make_user(status=UserStatus.inactive)]
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = users
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.search_users(tenant_id=tenant_id, status="inactive")
        assert result.is_ok()
        assert len(result.ok) == 1
        assert result.ok[0]["status"] == "inactive"

    @pytest.mark.anyio
    async def test_invalid_status_returns_empty(self, service, mock_session, tenant_id):
        """Invalid status string returns empty list instead of crashing."""
        result = await service.search_users(tenant_id=tenant_id, status="bogus")
        assert result.is_ok()
        assert result.ok == []


# ---------------------------------------------------------------------------
# OTel span tests
# ---------------------------------------------------------------------------


class TestOTelSpans:
    """Verify OTel spans are created with correct names and tenant.id attribute."""

    @pytest.mark.anyio
    async def test_create_user_span(self, service, mock_session, tenant_id):
        mock_session.flush = AsyncMock()
        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.create_user(tenant_id=tenant_id, email="a@b.com", user_name="a")

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.create_user",
                attributes={"tenant.id": str(tenant_id)},
            )

    @pytest.mark.anyio
    async def test_get_user_span(self, service, mock_session, tenant_id):
        uid = uuid.uuid4()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = _make_user(user_id=uid)
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.get_user(tenant_id=tenant_id, user_id=uid)

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.get_user",
                attributes={"tenant.id": str(tenant_id), "user.id": str(uid)},
            )

    @pytest.mark.anyio
    async def test_update_user_span(self, service, mock_session, tenant_id):
        user = _make_user()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.update_user(tenant_id=tenant_id, user_id=user.id, email="x@y.com")

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.update_user",
                attributes={"tenant.id": str(tenant_id), "user.id": str(user.id)},
            )

    @pytest.mark.anyio
    async def test_deactivate_user_span(self, service, mock_session, tenant_id):
        user = _make_user()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.deactivate_user(tenant_id=tenant_id, user_id=user.id)

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.deactivate_user",
                attributes={"tenant.id": str(tenant_id), "user.id": str(user.id)},
            )

    @pytest.mark.anyio
    async def test_search_users_span(self, service, mock_session, tenant_id):
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.search_users(tenant_id=tenant_id)

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.search_users",
                attributes={"tenant.id": str(tenant_id)},
            )


# ---------------------------------------------------------------------------
# Unimplemented methods raise NotImplementedError
# ---------------------------------------------------------------------------


# ===========================================================================
# Model helpers for Story 2.2
# ===========================================================================


def _make_role(
    *,
    role_id: uuid.UUID | None = None,
    name: str = "admin",
    description: str = "Administrator role",
    tenant_id: uuid.UUID | None = None,
) -> Role:
    return Role(
        id=role_id or uuid.uuid4(),
        name=name,
        description=description,
        tenant_id=tenant_id,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _make_permission(
    *,
    permission_id: uuid.UUID | None = None,
    name: str = "documents.write",
    description: str = "Write documents",
) -> Permission:
    return Permission(
        id=permission_id or uuid.uuid4(),
        name=name,
        description=description,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _make_tenant(
    *,
    tenant_id: uuid.UUID | None = None,
    name: str = "Acme Corp",
    domains: list[str] | None = None,
    status: TenantStatus = TenantStatus.active,
) -> Tenant:
    return Tenant(
        id=tenant_id or uuid.uuid4(),
        name=name,
        domains=domains or [],
        status=status,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ===========================================================================
# Role CRUD (AC-2.2.1)
# ===========================================================================


class TestCreateRole:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        mock_session.flush = AsyncMock()
        result = await service.create_role(tenant_id=uuid.uuid4(), name="editor", description="Can edit")
        assert result.is_ok()
        d = result.ok
        assert d["name"] == "editor"
        assert d["description"] == "Can edit"
        mock_session.add.assert_called_once()

    @pytest.mark.anyio
    async def test_global_role_no_tenant(self, service, mock_session):
        mock_session.flush = AsyncMock()
        result = await service.create_role(name="superadmin")
        assert result.is_ok()
        assert result.ok["tenant_id"] is None

    @pytest.mark.anyio
    async def test_duplicate_name_returns_conflict(self, service, mock_session):
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()
        tid = uuid.uuid4()
        result = await service.create_role(tenant_id=tid, name="admin")
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "admin" in result.error.message
        assert str(tid) in result.error.message

    @pytest.mark.anyio
    async def test_global_duplicate_conflict_message(self, service, mock_session):
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()
        result = await service.create_role(name="admin")
        assert result.is_error()
        assert "global scope" in result.error.message

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session):
        adapter = AsyncMock()
        adapter.sync_role = AsyncMock(return_value=Error(SyncError(message="timeout", operation="sync_role")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        mock_session.flush = AsyncMock()
        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.create_role(name="editor")
            assert result.is_ok()
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_includes_all_fields(self, service, mock_session):
        mock_session.flush = AsyncMock()
        result = await service.create_role(name="viewer", description="Read-only")
        d = result.ok
        assert "id" in d
        assert "created_at" in d
        assert "updated_at" in d


class TestGetRole:
    @pytest.mark.anyio
    async def test_found(self, service, mock_session):
        role = _make_role()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_role(role_id=role.id)
        assert result.is_ok()
        assert result.ok["name"] == role.name
        assert result.ok["id"] == str(role.id)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        rid = uuid.uuid4()
        result = await service.get_role(role_id=rid)
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert str(rid) in result.error.message


class TestUpdateRole:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        role = _make_role()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        result = await service.update_role(role_id=role.id, name="senior-admin")
        assert result.is_ok()
        assert role.name == "senior-admin"

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.update_role(role_id=uuid.uuid4(), name="new")
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_duplicate_name_conflict(self, service, mock_session):
        role = _make_role()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.update_role(role_id=role.id, name="taken")
        assert result.is_error()
        assert isinstance(result.error, Conflict)

    @pytest.mark.anyio
    async def test_partial_update_description_only(self, service, mock_session):
        role = _make_role(name="admin", description="Old desc")
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_role(role_id=role.id, description="New desc")
        assert role.name == "admin"  # unchanged
        assert role.description == "New desc"

    @pytest.mark.anyio
    async def test_updates_timestamp(self, service, mock_session):
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        role = _make_role()
        role.updated_at = old_time
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_role(role_id=role.id, name="changed")
        assert role.updated_at > old_time

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session):
        adapter = AsyncMock()
        adapter.sync_role = AsyncMock(return_value=Error(SyncError(message="timeout", operation="sync_role")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        role = _make_role()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.update_role(role_id=role.id, name="updated")
            assert result.is_ok()
            mock_logger.warning.assert_called_once()


class TestDeleteRole:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        role = _make_role()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        result = await service.delete_role(role_id=role.id)
        assert result.is_ok()
        assert result.ok is None
        mock_session.delete.assert_awaited_once_with(role)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.delete_role(role_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session):
        adapter = AsyncMock()
        adapter.delete_role = AsyncMock(return_value=Error(SyncError(message="timeout", operation="delete_role")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        role = _make_role()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.delete_role(role_id=role.id)
            assert result.is_ok()
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_integrity_error_returns_conflict(self, service, mock_session):
        role = _make_role()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock(side_effect=IntegrityError("fk", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.delete_role(role_id=role.id)
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "dependent" in result.error.message


# ===========================================================================
# Permission CRUD (AC-2.2.2)
# ===========================================================================


class TestCreatePermission:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        mock_session.flush = AsyncMock()
        result = await service.create_permission(name="documents.write", description="Write docs")
        assert result.is_ok()
        d = result.ok
        assert d["name"] == "documents.write"
        assert d["description"] == "Write docs"

    @pytest.mark.anyio
    async def test_duplicate_name_returns_conflict(self, service, mock_session):
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()
        result = await service.create_permission(name="documents.write")
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "documents.write" in result.error.message

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session):
        adapter = AsyncMock()
        adapter.sync_permission = AsyncMock(
            return_value=Error(SyncError(message="timeout", operation="sync_permission"))
        )
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        mock_session.flush = AsyncMock()
        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.create_permission(name="perm.test")
            assert result.is_ok()
            mock_logger.warning.assert_called_once()


class TestGetPermission:
    @pytest.mark.anyio
    async def test_found(self, service, mock_session):
        perm = _make_permission()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_permission(permission_id=perm.id)
        assert result.is_ok()
        assert result.ok["name"] == perm.name

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        pid = uuid.uuid4()
        result = await service.get_permission(permission_id=pid)
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert str(pid) in result.error.message


class TestUpdatePermission:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        perm = _make_permission()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        result = await service.update_permission(permission_id=perm.id, name="documents.admin")
        assert result.is_ok()
        assert perm.name == "documents.admin"

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.update_permission(permission_id=uuid.uuid4(), name="new")
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_duplicate_name_conflict(self, service, mock_session):
        perm = _make_permission()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.update_permission(permission_id=perm.id, name="taken")
        assert result.is_error()
        assert isinstance(result.error, Conflict)

    @pytest.mark.anyio
    async def test_partial_update_description_only(self, service, mock_session):
        perm = _make_permission(name="original", description="Old")
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_permission(permission_id=perm.id, description="New desc")
        assert perm.name == "original"  # unchanged
        assert perm.description == "New desc"

    @pytest.mark.anyio
    async def test_updates_timestamp(self, service, mock_session):
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        perm = _make_permission()
        perm.updated_at = old_time
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_permission(permission_id=perm.id, name="changed")
        assert perm.updated_at > old_time


class TestDeletePermission:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        perm = _make_permission()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        result = await service.delete_permission(permission_id=perm.id)
        assert result.is_ok()
        assert result.ok is None
        mock_session.delete.assert_awaited_once_with(perm)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.delete_permission(permission_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session):
        adapter = AsyncMock()
        adapter.delete_permission = AsyncMock(
            return_value=Error(SyncError(message="timeout", operation="delete_permission"))
        )
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        perm = _make_permission()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.delete_permission(permission_id=perm.id)
            assert result.is_ok()
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_integrity_error_returns_conflict(self, service, mock_session):
        perm = _make_permission()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock(side_effect=IntegrityError("fk", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.delete_permission(permission_id=perm.id)
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "dependent" in result.error.message


# ===========================================================================
# Permission mapping (AC-2.2.2)
# ===========================================================================


class TestMapPermissionToRole:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        role = _make_role()
        perm = _make_permission()

        # First execute returns role, second returns permission
        role_exec = MagicMock()
        role_exec.scalar_one_or_none.return_value = role
        perm_exec = MagicMock()
        perm_exec.scalar_one_or_none.return_value = perm

        mock_session.execute = AsyncMock(side_effect=[role_exec, perm_exec])
        mock_session.flush = AsyncMock()

        result = await service.map_permission_to_role(role_id=role.id, permission_id=perm.id)
        assert result.is_ok()
        assert result.ok is None
        mock_session.add.assert_called_once()

    @pytest.mark.anyio
    async def test_role_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.map_permission_to_role(role_id=uuid.uuid4(), permission_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Role" in result.error.message

    @pytest.mark.anyio
    async def test_permission_not_found(self, service, mock_session):
        role = _make_role()
        role_exec = MagicMock()
        role_exec.scalar_one_or_none.return_value = role
        perm_exec = MagicMock()
        perm_exec.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(side_effect=[role_exec, perm_exec])

        result = await service.map_permission_to_role(role_id=role.id, permission_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Permission" in result.error.message

    @pytest.mark.anyio
    async def test_duplicate_mapping_returns_conflict(self, service, mock_session):
        role = _make_role()
        perm = _make_permission()
        role_exec = MagicMock()
        role_exec.scalar_one_or_none.return_value = role
        perm_exec = MagicMock()
        perm_exec.scalar_one_or_none.return_value = perm

        mock_session.execute = AsyncMock(side_effect=[role_exec, perm_exec])
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.map_permission_to_role(role_id=role.id, permission_id=perm.id)
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "already mapped" in result.error.message


class TestUnmapPermissionFromRole:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        mapping = RolePermission(role_id=uuid.uuid4(), permission_id=uuid.uuid4())
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = mapping
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        result = await service.unmap_permission_from_role(role_id=mapping.role_id, permission_id=mapping.permission_id)
        assert result.is_ok()
        mock_session.delete.assert_awaited_once_with(mapping)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.unmap_permission_from_role(role_id=uuid.uuid4(), permission_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "not mapped" in result.error.message


# ===========================================================================
# Tenant CRUD (AC-2.2.3)
# ===========================================================================


class TestCreateTenant:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        mock_session.flush = AsyncMock()
        result = await service.create_tenant(name="Acme Corp")
        assert result.is_ok()
        d = result.ok
        assert d["name"] == "Acme Corp"
        assert d["domains"] == []
        assert d["status"] == "active"

    @pytest.mark.anyio
    async def test_with_domains(self, service, mock_session):
        mock_session.flush = AsyncMock()
        result = await service.create_tenant(name="Acme", domains=["acme.com", "acme.io"])
        assert result.is_ok()
        assert result.ok["domains"] == ["acme.com", "acme.io"]

    @pytest.mark.anyio
    async def test_duplicate_name_returns_conflict(self, service, mock_session):
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()
        result = await service.create_tenant(name="Acme")
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "Acme" in result.error.message

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session):
        adapter = AsyncMock()
        adapter.sync_tenant = AsyncMock(return_value=Error(SyncError(message="timeout", operation="sync_tenant")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        mock_session.flush = AsyncMock()
        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.create_tenant(name="Sync-fail Corp")
            assert result.is_ok()
            mock_logger.warning.assert_called_once()


class TestGetTenant:
    @pytest.mark.anyio
    async def test_found(self, service, mock_session):
        tenant = _make_tenant()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_tenant(tenant_id=tenant.id)
        assert result.is_ok()
        assert result.ok["name"] == tenant.name

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        tid = uuid.uuid4()
        result = await service.get_tenant(tenant_id=tid)
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert str(tid) in result.error.message


class TestUpdateTenant:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        tenant = _make_tenant()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        result = await service.update_tenant(tenant_id=tenant.id, name="New Acme")
        assert result.is_ok()
        assert tenant.name == "New Acme"

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.update_tenant(tenant_id=uuid.uuid4(), name="New")
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_duplicate_name_conflict(self, service, mock_session):
        tenant = _make_tenant()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.update_tenant(tenant_id=tenant.id, name="taken")
        assert result.is_error()
        assert isinstance(result.error, Conflict)

    @pytest.mark.anyio
    async def test_update_domains(self, service, mock_session):
        tenant = _make_tenant(domains=["old.com"])
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_tenant(tenant_id=tenant.id, domains=["new.com", "new.io"])
        assert tenant.domains == ["new.com", "new.io"]

    @pytest.mark.anyio
    async def test_partial_update_name_only(self, service, mock_session):
        tenant = _make_tenant(name="Old", domains=["old.com"])
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_tenant(tenant_id=tenant.id, name="New")
        assert tenant.name == "New"
        assert tenant.domains == ["old.com"]  # unchanged

    @pytest.mark.anyio
    async def test_updates_timestamp(self, service, mock_session):
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        tenant = _make_tenant()
        tenant.updated_at = old_time
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        await service.update_tenant(tenant_id=tenant.id, name="changed")
        assert tenant.updated_at > old_time

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session):
        adapter = AsyncMock()
        adapter.sync_tenant = AsyncMock(return_value=Error(SyncError(message="timeout", operation="sync_tenant")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        tenant = _make_tenant()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.update_tenant(tenant_id=tenant.id, name="updated")
            assert result.is_ok()
            mock_logger.warning.assert_called_once()


class TestDeleteTenant:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        tenant = _make_tenant()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        result = await service.delete_tenant(tenant_id=tenant.id)
        assert result.is_ok()
        assert result.ok is None
        mock_session.delete.assert_awaited_once_with(tenant)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.delete_tenant(tenant_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session):
        adapter = AsyncMock()
        adapter.delete_tenant = AsyncMock(return_value=Error(SyncError(message="timeout", operation="delete_tenant")))
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        tenant = _make_tenant()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.delete_tenant(tenant_id=tenant.id)
            assert result.is_ok()
            mock_logger.warning.assert_called_once()

    @pytest.mark.anyio
    async def test_integrity_error_returns_conflict(self, service, mock_session):
        tenant = _make_tenant()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = tenant
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock(side_effect=IntegrityError("fk", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.delete_tenant(tenant_id=tenant.id)
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "dependent" in result.error.message


# ===========================================================================
# User-tenant-role assignment (AC-2.2.4)
# ===========================================================================


class TestAssignRoleToUser:
    @staticmethod
    def _mock_validation_queries(mock_session, user=None, tenant=None, role=None):
        """Set up session.execute to return user, tenant, role for the 3 validation queries."""
        results = []
        for entity in [user, tenant, role]:
            res = MagicMock()
            res.scalar_one_or_none.return_value = entity
            results.append(res)
        mock_session.execute = AsyncMock(side_effect=results)

    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session, tenant_id):
        mock_session.flush = AsyncMock()
        user = _make_user()
        tenant = _make_tenant(tenant_id=tenant_id)
        role = _make_role()
        self._mock_validation_queries(mock_session, user=user, tenant=tenant, role=role)

        result = await service.assign_role_to_user(tenant_id=tenant_id, user_id=user.id, role_id=role.id)
        assert result.is_ok()
        assert result.ok is None
        mock_session.add.assert_called_once()

    @pytest.mark.anyio
    async def test_user_not_found(self, service, mock_session, tenant_id):
        self._mock_validation_queries(mock_session, user=None, tenant=_make_tenant(), role=_make_role())

        result = await service.assign_role_to_user(tenant_id=tenant_id, user_id=uuid.uuid4(), role_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "User" in result.error.message

    @pytest.mark.anyio
    async def test_tenant_not_found(self, service, mock_session, tenant_id):
        user = _make_user()
        user_res = MagicMock()
        user_res.scalar_one_or_none.return_value = user
        tenant_res = MagicMock()
        tenant_res.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(side_effect=[user_res, tenant_res])

        result = await service.assign_role_to_user(tenant_id=tenant_id, user_id=user.id, role_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Tenant" in result.error.message

    @pytest.mark.anyio
    async def test_role_not_found(self, service, mock_session, tenant_id):
        user = _make_user()
        tenant = _make_tenant(tenant_id=tenant_id)
        user_res = MagicMock()
        user_res.scalar_one_or_none.return_value = user
        tenant_res = MagicMock()
        tenant_res.scalar_one_or_none.return_value = tenant
        role_res = MagicMock()
        role_res.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(side_effect=[user_res, tenant_res, role_res])

        result = await service.assign_role_to_user(tenant_id=tenant_id, user_id=user.id, role_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Role" in result.error.message

    @pytest.mark.anyio
    async def test_duplicate_assignment_returns_conflict(self, service, mock_session, tenant_id):
        user = _make_user()
        tenant = _make_tenant(tenant_id=tenant_id)
        role = _make_role()
        self._mock_validation_queries(mock_session, user=user, tenant=tenant, role=role)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("dup", {}, None))
        mock_session.rollback = AsyncMock()

        result = await service.assign_role_to_user(tenant_id=tenant_id, user_id=user.id, role_id=role.id)
        assert result.is_error()
        assert isinstance(result.error, Conflict)
        assert "already assigned" in result.error.message
        assert str(role.id) in result.error.message
        assert str(user.id) in result.error.message
        assert str(tenant_id) in result.error.message

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session, tenant_id):
        adapter = AsyncMock()
        adapter.sync_role_assignment = AsyncMock(
            return_value=Error(SyncError(message="timeout", operation="sync_role_assignment"))
        )
        svc = PostgresIdentityService(session=mock_session, adapter=adapter)
        user = _make_user()
        tenant = _make_tenant(tenant_id=tenant_id)
        role = _make_role()
        results = []
        for entity in [user, tenant, role]:
            res = MagicMock()
            res.scalar_one_or_none.return_value = entity
            results.append(res)
        mock_session.execute = AsyncMock(side_effect=results)
        mock_session.flush = AsyncMock()

        with patch("app.services.identity_impl.logger") as mock_logger:
            result = await svc.assign_role_to_user(tenant_id=tenant_id, user_id=user.id, role_id=role.id)
            assert result.is_ok()
            mock_logger.warning.assert_called_once()


class TestRemoveRoleFromUser:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session, tenant_id):
        user_id = uuid.uuid4()
        role_id = uuid.uuid4()
        from app.models.identity.assignment import UserTenantRole

        assignment = UserTenantRole(user_id=user_id, tenant_id=tenant_id, role_id=role_id)
        assignment_result = MagicMock()
        assignment_result.scalar_one_or_none.return_value = assignment

        role = _make_role(role_id=role_id, name="admin")
        role_result = MagicMock()
        role_result.scalar_one_or_none.return_value = role

        mock_session.execute = AsyncMock(side_effect=[assignment_result, role_result])
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        result = await service.remove_role_from_user(tenant_id=tenant_id, user_id=user_id, role_id=role_id)
        assert result.is_ok()
        mock_session.delete.assert_awaited_once_with(assignment)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session, tenant_id):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.remove_role_from_user(tenant_id=tenant_id, user_id=uuid.uuid4(), role_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "not assigned" in result.error.message


class TestGetTenantUsersWithRoles:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        tenant = _make_tenant()
        user1 = _make_user(email="alice@test.com", user_name="alice")
        user2 = _make_user(email="bob@test.com", user_name="bob")
        role_admin = _make_role(name="admin")
        role_viewer = _make_role(name="viewer")
        assignment1 = MagicMock()
        assignment2 = MagicMock()
        assignment3 = MagicMock()

        # Tenant check returns tenant
        tenant_exec = MagicMock()
        tenant_exec.scalar_one_or_none.return_value = tenant

        # Users query returns rows
        users_exec = MagicMock()
        users_exec.all.return_value = [
            (user1, role_admin, assignment1),
            (user1, role_viewer, assignment2),  # same user, different role
            (user2, role_viewer, assignment3),
        ]

        mock_session.execute = AsyncMock(side_effect=[tenant_exec, users_exec])

        result = await service.get_tenant_users_with_roles(tenant_id=tenant.id)
        assert result.is_ok()
        users = result.ok
        assert len(users) == 2

        # User1 should have 2 roles
        alice = next(u for u in users if u["email"] == "alice@test.com")
        assert len(alice["roles"]) == 2

        # User2 should have 1 role
        bob = next(u for u in users if u["email"] == "bob@test.com")
        assert len(bob["roles"]) == 1

    @pytest.mark.anyio
    async def test_tenant_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_tenant_users_with_roles(tenant_id=uuid.uuid4())
        assert result.is_error()
        assert isinstance(result.error, NotFound)

    @pytest.mark.anyio
    async def test_empty_tenant(self, service, mock_session):
        tenant = _make_tenant()
        tenant_exec = MagicMock()
        tenant_exec.scalar_one_or_none.return_value = tenant
        users_exec = MagicMock()
        users_exec.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[tenant_exec, users_exec])

        result = await service.get_tenant_users_with_roles(tenant_id=tenant.id)
        assert result.is_ok()
        assert result.ok == []


# ===========================================================================
# OTel spans for Story 2.2 methods
# ===========================================================================


class TestOTelSpansStory22:
    """Verify OTel spans are created with correct names and attributes for Story 2.2 methods."""

    @pytest.mark.anyio
    async def test_create_role_span(self, service, mock_session):
        mock_session.flush = AsyncMock()
        tid = uuid.uuid4()
        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.create_role(tenant_id=tid, name="admin")

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.create_role",
                attributes={"tenant.id": str(tid)},
            )

    @pytest.mark.anyio
    async def test_create_role_global_span(self, service, mock_session):
        mock_session.flush = AsyncMock()
        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.create_role(name="global-role")

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.create_role",
                attributes={"tenant.id": "global"},
            )

    @pytest.mark.anyio
    async def test_get_role_span(self, service, mock_session):
        role = _make_role()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)

        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.get_role(role_id=role.id)

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.get_role",
                attributes={"role.id": str(role.id)},
            )

    @pytest.mark.anyio
    async def test_create_permission_span(self, service, mock_session):
        mock_session.flush = AsyncMock()
        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.create_permission(name="docs.write")

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.create_permission",
                attributes={"permission.name": "docs.write"},
            )

    @pytest.mark.anyio
    async def test_create_tenant_span(self, service, mock_session):
        mock_session.flush = AsyncMock()
        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.create_tenant(name="Acme")

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.create_tenant",
                attributes={"tenant.name": "Acme"},
            )

    @pytest.mark.anyio
    async def test_assign_role_to_user_span(self, service, mock_session, tenant_id):
        mock_session.flush = AsyncMock()
        user = _make_user()
        role = _make_role()
        tenant = _make_tenant(tenant_id=tenant_id)
        results = []
        for entity in [user, tenant, role]:
            res = MagicMock()
            res.scalar_one_or_none.return_value = entity
            results.append(res)
        mock_session.execute = AsyncMock(side_effect=results)

        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.assign_role_to_user(tenant_id=tenant_id, user_id=user.id, role_id=role.id)

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.assign_role_to_user",
                attributes={
                    "tenant.id": str(tenant_id),
                    "user.id": str(user.id),
                    "role.id": str(role.id),
                },
            )

    @pytest.mark.anyio
    async def test_get_tenant_users_with_roles_span(self, service, mock_session):
        tenant = _make_tenant()
        tenant_exec = MagicMock()
        tenant_exec.scalar_one_or_none.return_value = tenant
        users_exec = MagicMock()
        users_exec.all.return_value = []
        mock_session.execute = AsyncMock(side_effect=[tenant_exec, users_exec])

        with patch("app.services.identity_impl.tracer") as mock_tracer:
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value = mock_span
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=False)

            await service.get_tenant_users_with_roles(tenant_id=tenant.id)

            mock_tracer.start_as_current_span.assert_called_once_with(
                "identity.get_tenant_users_with_roles",
                attributes={"tenant.id": str(tenant.id)},
            )


# ---------------------------------------------------------------------------
# user_to_dict serialization
# ---------------------------------------------------------------------------


class TestUserToDict:
    def test_serializes_all_fields(self, service):
        user = _make_user()
        d = service._user_to_dict(user)
        assert d["id"] == str(user.id)
        assert d["email"] == user.email
        assert d["user_name"] == user.user_name
        assert d["given_name"] == user.given_name
        assert d["family_name"] == user.family_name
        assert d["status"] == "active"
        assert d["created_at"] is not None
        assert d["updated_at"] is not None

    def test_status_enum_to_string(self, service):
        user = _make_user(status=UserStatus.inactive)
        d = service._user_to_dict(user)
        assert d["status"] == "inactive"

    def test_none_timestamps(self, service):
        user = _make_user()
        user.created_at = None
        user.updated_at = None
        d = service._user_to_dict(user)
        assert d["created_at"] is None
        assert d["updated_at"] is None


# ---------------------------------------------------------------------------
# _role_to_dict serialization
# ---------------------------------------------------------------------------


class TestRoleToDict:
    def test_serializes_all_fields(self, service):
        role = _make_role()
        d = service._role_to_dict(role)
        assert d["id"] == str(role.id)
        assert d["name"] == role.name
        assert d["description"] == role.description
        assert d["tenant_id"] is None
        assert d["created_at"] is not None
        assert d["updated_at"] is not None

    def test_with_tenant_id(self, service):
        tid = uuid.uuid4()
        role = _make_role(tenant_id=tid)
        d = service._role_to_dict(role)
        assert d["tenant_id"] == str(tid)

    def test_none_timestamps(self, service):
        role = _make_role()
        role.created_at = None
        role.updated_at = None
        d = service._role_to_dict(role)
        assert d["created_at"] is None
        assert d["updated_at"] is None


# ---------------------------------------------------------------------------
# _permission_to_dict serialization
# ---------------------------------------------------------------------------


class TestPermissionToDict:
    def test_serializes_all_fields(self, service):
        perm = _make_permission()
        d = service._permission_to_dict(perm)
        assert d["id"] == str(perm.id)
        assert d["name"] == perm.name
        assert d["description"] == perm.description
        assert d["created_at"] is not None
        assert d["updated_at"] is not None

    def test_none_timestamps(self, service):
        perm = _make_permission()
        perm.created_at = None
        perm.updated_at = None
        d = service._permission_to_dict(perm)
        assert d["created_at"] is None
        assert d["updated_at"] is None


# ---------------------------------------------------------------------------
# _tenant_to_dict serialization
# ---------------------------------------------------------------------------


class TestTenantToDict:
    def test_serializes_all_fields(self, service):
        tenant = _make_tenant(domains=["acme.com"])
        d = service._tenant_to_dict(tenant)
        assert d["id"] == str(tenant.id)
        assert d["name"] == tenant.name
        assert d["domains"] == ["acme.com"]
        assert d["status"] == "active"
        assert d["created_at"] is not None
        assert d["updated_at"] is not None

    def test_status_enum_to_string(self, service):
        tenant = _make_tenant(status=TenantStatus.suspended)
        d = service._tenant_to_dict(tenant)
        assert d["status"] == "suspended"

    def test_none_domains_becomes_empty_list(self, service):
        tenant = _make_tenant()
        tenant.domains = None
        d = service._tenant_to_dict(tenant)
        assert d["domains"] == []

    def test_none_timestamps(self, service):
        tenant = _make_tenant()
        tenant.created_at = None
        tenant.updated_at = None
        d = service._tenant_to_dict(tenant)
        assert d["created_at"] is None
        assert d["updated_at"] is None


# ---------------------------------------------------------------------------
# PostgresIdentityService is concrete IdentityService
# ---------------------------------------------------------------------------


class TestPostgresIdentityServiceIsConcreteIdentityService:
    def test_is_subclass_of_identity_service(self):
        from app.services.identity import IdentityService

        assert issubclass(PostgresIdentityService, IdentityService)

    def test_can_instantiate(self, mock_session, noop_adapter):
        svc = PostgresIdentityService(session=mock_session, adapter=noop_adapter)
        assert svc is not None


# ===========================================================================
# Lookup operations (Story 2.3)
# ===========================================================================


class TestListRoles:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        roles = [_make_role(name="admin"), _make_role(name="viewer")]
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = roles
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.list_roles()
        assert result.is_ok()
        assert len(result.ok) == 2
        names = {r["name"] for r in result.ok}
        assert names == {"admin", "viewer"}

    @pytest.mark.anyio
    async def test_with_tenant_filter(self, service, mock_session):
        tid = uuid.uuid4()
        roles = [_make_role(name="editor", tenant_id=tid)]
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = roles
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.list_roles(tenant_id=tid)
        assert result.is_ok()
        assert len(result.ok) == 1

    @pytest.mark.anyio
    async def test_empty_results(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.list_roles()
        assert result.is_ok()
        assert result.ok == []


class TestListPermissions:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session):
        perms = [_make_permission(name="docs.read"), _make_permission(name="docs.write")]
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = perms
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.list_permissions()
        assert result.is_ok()
        assert len(result.ok) == 2

    @pytest.mark.anyio
    async def test_empty_results(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.list_permissions()
        assert result.is_ok()
        assert result.ok == []


class TestGetRoleByName:
    @pytest.mark.anyio
    async def test_found(self, service, mock_session):
        role = _make_role(name="editor")
        exec_result = MagicMock()
        exec_result.scalars.return_value.first.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_role_by_name(name="editor")
        assert result.is_ok()
        assert result.ok["name"] == "editor"
        assert result.ok["id"] == str(role.id)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalars.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_role_by_name(name="nonexistent")
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "nonexistent" in result.error.message

    @pytest.mark.anyio
    async def test_with_tenant_filter(self, service, mock_session):
        tid = uuid.uuid4()
        role = _make_role(name="editor", tenant_id=tid)
        exec_result = MagicMock()
        exec_result.scalars.return_value.first.return_value = role
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_role_by_name(name="editor", tenant_id=tid)
        assert result.is_ok()
        assert result.ok["name"] == "editor"


class TestGetPermissionByName:
    @pytest.mark.anyio
    async def test_found(self, service, mock_session):
        perm = _make_permission(name="docs.write")
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = perm
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_permission_by_name(name="docs.write")
        assert result.is_ok()
        assert result.ok["name"] == "docs.write"
        assert result.ok["id"] == str(perm.id)

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        result = await service.get_permission_by_name(name="nonexistent")
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "nonexistent" in result.error.message


class TestActivateUser:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session, tenant_id):
        user = _make_user(status=UserStatus.inactive)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        result = await service.activate_user(tenant_id=tenant_id, user_id=user.id)
        assert result.is_ok()
        assert result.ok["status"] == "active"
        assert user.status == UserStatus.active

    @pytest.mark.anyio
    async def test_not_found(self, service, mock_session, tenant_id):
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=exec_result)

        uid = uuid.uuid4()
        result = await service.activate_user(tenant_id=tenant_id, user_id=uid)
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert str(uid) in result.error.message

    @pytest.mark.anyio
    async def test_integrity_error_returns_conflict(self, service, mock_session, tenant_id):
        user = _make_user(status=UserStatus.inactive)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock(side_effect=IntegrityError("", {}, Exception()))
        mock_session.rollback = AsyncMock()

        result = await service.activate_user(tenant_id=tenant_id, user_id=user.id)
        assert result.is_error()
        assert isinstance(result.error, Conflict)

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session, tenant_id):
        """activate_user succeeds even when adapter sync fails."""
        user = _make_user(status=UserStatus.inactive)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = user
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.flush = AsyncMock()

        failing_adapter = AsyncMock()
        failing_adapter.sync_user.return_value = Error(
            SyncError(message="timeout", operation="sync_user"),
        )
        svc = PostgresIdentityService(session=mock_session, adapter=failing_adapter)

        result = await svc.activate_user(tenant_id=tenant_id, user_id=user.id)
        assert result.is_ok()


class TestRemoveUserFromTenant:
    @pytest.mark.anyio
    async def test_happy_path(self, service, mock_session, tenant_id):
        from app.models.identity.assignment import UserTenantRole

        assignment = MagicMock(spec=UserTenantRole)
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [assignment]
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        uid = uuid.uuid4()
        result = await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=uid)
        assert result.is_ok()
        assert result.ok is None
        mock_session.delete.assert_called_once_with(assignment)

    @pytest.mark.anyio
    async def test_no_assignments_returns_not_found(self, service, mock_session, tenant_id):
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=exec_result)

        uid = uuid.uuid4()
        result = await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=uid)
        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert str(uid) in result.error.message

    @pytest.mark.anyio
    async def test_integrity_error_returns_conflict(self, service, mock_session, tenant_id):
        from app.models.identity.assignment import UserTenantRole

        assignment = MagicMock(spec=UserTenantRole)
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [assignment]
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock(side_effect=IntegrityError("", {}, Exception()))
        mock_session.rollback = AsyncMock()

        uid = uuid.uuid4()
        result = await service.remove_user_from_tenant(tenant_id=tenant_id, user_id=uid)
        assert result.is_error()
        assert isinstance(result.error, Conflict)

    @pytest.mark.anyio
    async def test_sync_failure_still_ok(self, mock_session, tenant_id):
        from app.models.identity.assignment import UserTenantRole

        assignment = MagicMock(spec=UserTenantRole)
        exec_result = MagicMock()
        exec_result.scalars.return_value.all.return_value = [assignment]
        mock_session.execute = AsyncMock(return_value=exec_result)
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        failing_adapter = AsyncMock()
        failing_adapter.remove_user_from_tenant.return_value = Error(
            SyncError(message="timeout", operation="remove_user_from_tenant"),
        )
        svc = PostgresIdentityService(session=mock_session, adapter=failing_adapter)

        uid = uuid.uuid4()
        result = await svc.remove_user_from_tenant(tenant_id=tenant_id, user_id=uid)
        assert result.is_ok()
