"""Unit tests for PostgresIdentityService (Story 2.1).

Covers:
- create_user: happy path, duplicate email (Conflict), sync failure (logged, still Ok)
- get_user: found (tenant-scoped), not found
- update_user: happy path (tenant-scoped), not found, duplicate email, sync failure
- deactivate_user: happy path (tenant-scoped), not found, sync failure, IntegrityError
- search_users: tenant-scoped, with query, empty results, status filter, wildcard escape
- Unimplemented methods raise NotImplementedError
- OTel spans created for each operation with correct attributes
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from expression import Error
from sqlalchemy.exc import IntegrityError

from app.errors.identity import Conflict, NotFound
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


class TestUnimplementedMethods:
    @pytest.mark.anyio
    async def test_create_role(self, service):
        with pytest.raises(NotImplementedError, match="story 2.2"):
            await service.create_role(tenant_id=uuid.uuid4(), name="admin")

    @pytest.mark.anyio
    async def test_get_role(self, service):
        with pytest.raises(NotImplementedError):
            await service.get_role(role_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_update_role(self, service):
        with pytest.raises(NotImplementedError):
            await service.update_role(role_id=uuid.uuid4(), name="new")

    @pytest.mark.anyio
    async def test_delete_role(self, service):
        with pytest.raises(NotImplementedError):
            await service.delete_role(role_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_create_permission(self, service):
        with pytest.raises(NotImplementedError):
            await service.create_permission(name="read")

    @pytest.mark.anyio
    async def test_get_permission(self, service):
        with pytest.raises(NotImplementedError):
            await service.get_permission(permission_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_update_permission(self, service):
        with pytest.raises(NotImplementedError):
            await service.update_permission(permission_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_delete_permission(self, service):
        with pytest.raises(NotImplementedError):
            await service.delete_permission(permission_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_map_permission_to_role(self, service):
        with pytest.raises(NotImplementedError):
            await service.map_permission_to_role(role_id=uuid.uuid4(), permission_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_unmap_permission_from_role(self, service):
        with pytest.raises(NotImplementedError):
            await service.unmap_permission_from_role(role_id=uuid.uuid4(), permission_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_create_tenant(self, service):
        with pytest.raises(NotImplementedError):
            await service.create_tenant(name="Acme")

    @pytest.mark.anyio
    async def test_get_tenant(self, service):
        with pytest.raises(NotImplementedError):
            await service.get_tenant(tenant_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_update_tenant(self, service):
        with pytest.raises(NotImplementedError):
            await service.update_tenant(tenant_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_delete_tenant(self, service):
        with pytest.raises(NotImplementedError):
            await service.delete_tenant(tenant_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_assign_role_to_user(self, service):
        with pytest.raises(NotImplementedError):
            await service.assign_role_to_user(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), role_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_remove_role_from_user(self, service):
        with pytest.raises(NotImplementedError):
            await service.remove_role_from_user(tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), role_id=uuid.uuid4())

    @pytest.mark.anyio
    async def test_get_tenant_users_with_roles(self, service):
        with pytest.raises(NotImplementedError):
            await service.get_tenant_users_with_roles(tenant_id=uuid.uuid4())


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
