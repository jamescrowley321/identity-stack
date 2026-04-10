"""Unit tests for IdentityResolutionService (Story 4.3).

Tests cover:
- AC-4.3.1: resolve() happy path — provider lookup, IdP link, user, build payload
- AC-4.3.2: Redis cache hit returns cached data; cache miss writes back
- AC-4.3.2: Redis unavailable → graceful degradation (skip cache, query Postgres)
- AC-4.3.3: Cache invalidation subscriber — user-scoped + broad invalidation
- Edge cases: provider not found, link not found, user not found
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors.identity import NotFound
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.identity_resolution import (
    IDENTITY_CACHE_TTL,
    IdentityResolutionService,
    _handle_invalidation_event,
    run_cache_invalidation_subscriber,
)


def _make_redis_mock():
    """Create a properly structured Redis mock.

    redis.asyncio.Redis.pipeline() and .pubsub() are synchronous methods
    that return pipeline/pubsub objects. pipeline methods (setex, sadd, etc.)
    are synchronous queue-adds; only execute() is async.
    """
    redis = AsyncMock()

    # pipeline() is sync, returns a MagicMock with sync queue methods + async execute
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis.pipeline = MagicMock(return_value=pipe)

    return redis, pipe


def _make_pubsub_mock():
    """Create a properly structured pubsub mock.

    redis.pubsub() is synchronous. Subscribe/unsubscribe/listen/aclose are async.
    """
    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()
    return pubsub


# --- Helpers ---


def _make_user(**overrides):
    """Create a mock user entity."""
    user = MagicMock()
    user.id = overrides.get("id", uuid.uuid4())
    user.email = overrides.get("email", "alice@example.com")
    user.user_name = overrides.get("user_name", "alice")
    user.given_name = overrides.get("given_name", "Alice")
    user.family_name = overrides.get("family_name", "Smith")
    user.status = MagicMock()
    user.status.value = overrides.get("status", "active")
    return user


def _make_provider(**overrides):
    provider = MagicMock()
    provider.id = overrides.get("id", uuid.uuid4())
    provider.name = overrides.get("name", "descope")
    return provider


def _make_link(**overrides):
    link = MagicMock()
    link.id = overrides.get("id", uuid.uuid4())
    link.user_id = overrides.get("user_id", uuid.uuid4())
    link.provider_id = overrides.get("provider_id", uuid.uuid4())
    link.external_sub = overrides.get("external_sub", "ext-sub-123")
    return link


def _make_assignment(**overrides):
    assignment = MagicMock()
    assignment.tenant_id = overrides.get("tenant_id", uuid.uuid4())
    assignment.role_id = overrides.get("role_id", uuid.uuid4())
    return assignment


def _make_role(**overrides):
    role = MagicMock()
    role.id = overrides.get("id", uuid.uuid4())
    role.name = overrides.get("name", "admin")
    return role


def _make_permission(**overrides):
    perm = MagicMock()
    perm.name = overrides.get("name", "read:users")
    return perm


def _make_tenant(**overrides):
    tenant = MagicMock()
    tenant.id = overrides.get("id", uuid.uuid4())
    tenant.name = overrides.get("name", "Acme Corp")
    return tenant


def _build_service(*, redis_client=None):
    """Build an IdentityResolutionService with all mocked repositories."""
    user_repo = AsyncMock(spec=UserRepository)
    idp_link_repo = AsyncMock(spec=IdPLinkRepository)
    provider_repo = AsyncMock(spec=ProviderRepository)
    assignment_repo = AsyncMock(spec=UserTenantRoleRepository)
    role_repo = AsyncMock(spec=RoleRepository)
    tenant_repo = AsyncMock(spec=TenantRepository)

    service = IdentityResolutionService(
        user_repository=user_repo,
        idp_link_repository=idp_link_repo,
        provider_repository=provider_repo,
        assignment_repository=assignment_repo,
        role_repository=role_repo,
        tenant_repository=tenant_repo,
        redis_client=redis_client,
    )
    return service, user_repo, idp_link_repo, provider_repo, assignment_repo, role_repo, tenant_repo


# --- AC-4.3.1: resolve() ---


@pytest.mark.anyio
class TestResolveHappyPath:
    """AC-4.3.1: resolve() builds canonical identity payload."""

    async def test_resolve_returns_full_payload(self):
        """Resolve builds user + roles + tenant memberships + linked IdPs."""
        service, user_repo, idp_link_repo, provider_repo, assignment_repo, role_repo, tenant_repo = _build_service()

        user_id = uuid.uuid4()
        provider_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        role_id = uuid.uuid4()
        user = _make_user(id=user_id)
        provider = _make_provider(id=provider_id, name="descope")
        link = _make_link(user_id=user_id, provider_id=provider_id, external_sub="ext-123")
        assignment = _make_assignment(tenant_id=tenant_id, role_id=role_id)
        role = _make_role(id=role_id, name="admin")
        perm = _make_permission(name="read:users")
        tenant = _make_tenant(id=tenant_id, name="Acme")

        provider_repo.get_by_name.return_value = provider
        idp_link_repo.get_by_provider_name_and_sub.return_value = link
        user_repo.get.return_value = user
        assignment_repo.list_by_user.return_value = [assignment]
        role_repo.get.return_value = role
        role_repo.get_permissions.return_value = [perm]
        tenant_repo.get.return_value = tenant
        idp_link_repo.get_by_user.return_value = [link]
        provider_repo.get.return_value = provider

        result = await service.resolve(provider="descope", sub="ext-123")

        assert result.is_ok()
        payload = result.ok
        assert payload["user"]["id"] == str(user_id)
        assert payload["user"]["email"] == "alice@example.com"
        assert len(payload["roles"]) == 1
        assert payload["roles"][0]["role_name"] == "admin"
        assert payload["roles"][0]["permissions"] == ["read:users"]
        assert payload["roles"][0]["tenant_id"] == str(tenant_id)
        assert len(payload["tenant_memberships"]) == 1
        assert payload["tenant_memberships"][0]["tenant_name"] == "Acme"
        assert len(payload["linked_idps"]) == 1
        assert payload["linked_idps"][0]["provider_name"] == "descope"

    async def test_resolve_user_with_no_roles(self):
        """User with no role assignments returns empty roles and memberships."""
        service, user_repo, idp_link_repo, provider_repo, assignment_repo, role_repo, tenant_repo = _build_service()

        user_id = uuid.uuid4()
        user = _make_user(id=user_id)
        provider = _make_provider()
        link = _make_link(user_id=user_id)

        provider_repo.get_by_name.return_value = provider
        idp_link_repo.get_by_provider_name_and_sub.return_value = link
        user_repo.get.return_value = user
        assignment_repo.list_by_user.return_value = []
        idp_link_repo.get_by_user.return_value = [link]
        provider_repo.get.return_value = provider

        result = await service.resolve(provider="descope", sub="ext-sub-123")

        assert result.is_ok()
        assert result.ok["roles"] == []
        assert result.ok["tenant_memberships"] == []

    async def test_resolve_user_with_multiple_tenants(self):
        """User assigned to multiple tenants returns all memberships."""
        service, user_repo, idp_link_repo, provider_repo, assignment_repo, role_repo, tenant_repo = _build_service()

        user_id = uuid.uuid4()
        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()
        role_a = uuid.uuid4()
        role_b = uuid.uuid4()

        provider_repo.get_by_name.return_value = _make_provider()
        idp_link_repo.get_by_provider_name_and_sub.return_value = _make_link(user_id=user_id)
        user_repo.get.return_value = _make_user(id=user_id)
        assignment_repo.list_by_user.return_value = [
            _make_assignment(tenant_id=tenant_a, role_id=role_a),
            _make_assignment(tenant_id=tenant_b, role_id=role_b),
        ]
        role_repo.get.side_effect = [
            _make_role(id=role_a, name="viewer"),
            _make_role(id=role_b, name="editor"),
        ]
        role_repo.get_permissions.side_effect = [[], [_make_permission(name="write:docs")]]
        tenant_repo.get.side_effect = [
            _make_tenant(id=tenant_a, name="Acme"),
            _make_tenant(id=tenant_b, name="Beta"),
        ]
        idp_link_repo.get_by_user.return_value = []

        result = await service.resolve(provider="descope", sub="ext-123")

        assert result.is_ok()
        assert len(result.ok["roles"]) == 2
        assert len(result.ok["tenant_memberships"]) == 2
        names = {m["tenant_name"] for m in result.ok["tenant_memberships"]}
        assert names == {"Acme", "Beta"}


@pytest.mark.anyio
class TestResolveErrors:
    """AC-4.3.1: resolve() returns NotFound for missing entities."""

    async def test_provider_not_found(self):
        service, *_ = _build_service()
        service._provider_repository.get_by_name.return_value = None

        result = await service.resolve(provider="unknown", sub="ext-123")

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Provider" in result.error.message

    async def test_idp_link_not_found(self):
        service, *_ = _build_service()
        service._provider_repository.get_by_name.return_value = _make_provider()
        service._idp_link_repository.get_by_provider_name_and_sub.return_value = None

        result = await service.resolve(provider="descope", sub="unknown-sub")

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "No identity found" in result.error.message

    async def test_linked_user_not_found(self):
        """FK integrity violation — user deleted but link remains."""
        service, *_ = _build_service()
        service._provider_repository.get_by_name.return_value = _make_provider()
        service._idp_link_repository.get_by_provider_name_and_sub.return_value = _make_link()
        service._user_repository.get.return_value = None

        result = await service.resolve(provider="descope", sub="ext-123")

        assert result.is_error()
        assert isinstance(result.error, NotFound)
        assert "Linked user" in result.error.message


# --- AC-4.3.2: Redis cache ---


@pytest.mark.anyio
class TestRedisCache:
    """AC-4.3.2: Redis cache read/write with configurable TTL."""

    async def test_cache_hit_returns_cached_data(self):
        """Cache hit returns deserialized JSON without querying Postgres."""
        redis = AsyncMock()
        cached_payload = {"user": {"id": "123"}, "roles": [], "tenant_memberships": [], "linked_idps": []}
        redis.get.return_value = json.dumps(cached_payload)

        service, user_repo, idp_link_repo, *_ = _build_service(redis_client=redis)

        result = await service.resolve(provider="descope", sub="ext-123")

        assert result.is_ok()
        assert result.ok == cached_payload
        # Cache key uses URL-encoding for dynamic parts
        expected_key = IdentityResolutionService._cache_key("descope", "ext-123")
        redis.get.assert_awaited_once_with(expected_key)
        # Should NOT have queried Postgres
        idp_link_repo.get_by_provider_name_and_sub.assert_not_awaited()

    async def test_cache_miss_writes_result_to_redis(self):
        """Cache miss queries Postgres and writes result to Redis."""
        redis, pipe = _make_redis_mock()
        redis.get.return_value = None

        service, user_repo, idp_link_repo, provider_repo, assignment_repo, role_repo, tenant_repo = _build_service(
            redis_client=redis
        )

        user_id = uuid.uuid4()
        user = _make_user(id=user_id)
        provider_repo.get_by_name.return_value = _make_provider()
        idp_link_repo.get_by_provider_name_and_sub.return_value = _make_link(user_id=user_id)
        user_repo.get.return_value = user
        assignment_repo.list_by_user.return_value = []
        idp_link_repo.get_by_user.return_value = []

        result = await service.resolve(provider="descope", sub="ext-123")

        assert result.is_ok()
        # Should have created an atomic pipeline
        redis.pipeline.assert_called_once_with(transaction=True)
        # Should have written to Redis pipeline with correct TTL
        expected_key = IdentityResolutionService._cache_key("descope", "ext-123")
        pipe.setex.assert_called_once_with(expected_key, IDENTITY_CACHE_TTL, pipe.setex.call_args[0][2])
        pipe.sadd.assert_called()
        pipe.execute.assert_awaited_once()

    async def test_cache_read_failure_degrades_gracefully(self):
        """Redis read failure → skip cache, query Postgres directly."""
        redis, pipe = _make_redis_mock()
        redis.get.side_effect = ConnectionError("Redis gone")

        service, user_repo, idp_link_repo, provider_repo, assignment_repo, *_ = _build_service(redis_client=redis)

        user_id = uuid.uuid4()
        provider_repo.get_by_name.return_value = _make_provider()
        idp_link_repo.get_by_provider_name_and_sub.return_value = _make_link(user_id=user_id)
        user_repo.get.return_value = _make_user(id=user_id)
        assignment_repo.list_by_user.return_value = []
        idp_link_repo.get_by_user.return_value = []

        result = await service.resolve(provider="descope", sub="ext-123")

        assert result.is_ok()
        # Still queried Postgres after cache failure
        idp_link_repo.get_by_provider_name_and_sub.assert_awaited_once()

    async def test_cache_write_failure_still_returns_ok(self):
        """Redis write failure doesn't affect the response."""
        redis, pipe = _make_redis_mock()
        redis.get.return_value = None
        pipe.execute.side_effect = ConnectionError("Redis gone")

        service, user_repo, idp_link_repo, provider_repo, assignment_repo, *_ = _build_service(redis_client=redis)

        user_id = uuid.uuid4()
        provider_repo.get_by_name.return_value = _make_provider()
        idp_link_repo.get_by_provider_name_and_sub.return_value = _make_link(user_id=user_id)
        user_repo.get.return_value = _make_user(id=user_id)
        assignment_repo.list_by_user.return_value = []
        idp_link_repo.get_by_user.return_value = []

        result = await service.resolve(provider="descope", sub="ext-123")

        assert result.is_ok()

    async def test_no_redis_client_skips_cache(self):
        """Service without Redis client goes directly to Postgres."""
        service, user_repo, idp_link_repo, provider_repo, assignment_repo, *_ = _build_service(redis_client=None)

        user_id = uuid.uuid4()
        provider_repo.get_by_name.return_value = _make_provider()
        idp_link_repo.get_by_provider_name_and_sub.return_value = _make_link(user_id=user_id)
        user_repo.get.return_value = _make_user(id=user_id)
        assignment_repo.list_by_user.return_value = []
        idp_link_repo.get_by_user.return_value = []

        result = await service.resolve(provider="descope", sub="ext-123")

        assert result.is_ok()
        idp_link_repo.get_by_provider_name_and_sub.assert_awaited_once()

    async def test_cache_key_escapes_colons(self):
        """Cache key URL-encodes dynamic parts to prevent key collision."""
        # Two different (provider, sub) pairs that would collide without encoding
        key1 = IdentityResolutionService._cache_key("a:b", "c")
        key2 = IdentityResolutionService._cache_key("a", "b:c")
        assert key1 != key2
        # Colons in dynamic parts are encoded
        assert "%3A" in key1
        assert "%3A" in key2


# --- AC-4.3.3: Cache invalidation subscriber ---


@pytest.mark.anyio
class TestHandleInvalidationEvent:
    """AC-4.3.3: _handle_invalidation_event invalidates cache entries."""

    async def test_user_event_deletes_user_cache_keys(self):
        """User change event deletes all cache entries for that user via reverse index."""
        redis, pipe = _make_redis_mock()
        user_id = str(uuid.uuid4())
        cache_keys = {b"identity:descope:ext-123", b"identity:google:goog-456"}
        redis.smembers.return_value = cache_keys

        await _handle_invalidation_event(redis, {"entity_type": "user", "entity_id": user_id})

        redis.smembers.assert_awaited_once_with(f"identity:user:{user_id}")
        # Should use atomic pipeline
        redis.pipeline.assert_called_once_with(transaction=True)
        # Should delete each cache key + remove from master set + delete reverse key
        assert pipe.delete.call_count >= 3  # 2 cache keys + 1 reverse key
        pipe.execute.assert_awaited_once()

    async def test_user_event_rejects_invalid_entity_id(self):
        """User event with non-UUID entity_id is rejected."""
        redis, pipe = _make_redis_mock()

        await _handle_invalidation_event(redis, {"entity_type": "user", "entity_id": "not-a-uuid"})

        redis.smembers.assert_not_awaited()
        redis.pipeline.assert_not_called()

    async def test_user_event_no_cache_keys_is_noop(self):
        """User change event with no cached entries does nothing."""
        redis, pipe = _make_redis_mock()
        redis.smembers.return_value = set()

        await _handle_invalidation_event(redis, {"entity_type": "user", "entity_id": str(uuid.uuid4())})

        redis.pipeline.assert_not_called()

    async def test_role_event_triggers_broad_invalidation(self):
        """Role change event deletes all identity cache keys + reverse indexes."""
        redis, pipe = _make_redis_mock()
        all_keys = {b"identity:descope:ext-1", b"identity:google:goog-2"}
        redis.smembers.return_value = all_keys
        # Mock scan to return reverse-index keys
        redis.scan.return_value = (0, [b"identity:user:abc-123"])

        await _handle_invalidation_event(redis, {"entity_type": "role", "entity_id": str(uuid.uuid4())})

        redis.smembers.assert_awaited_once_with("identity:all-keys")
        redis.pipeline.assert_called_once_with(transaction=True)
        # Should delete cache keys + reverse-index keys + master set
        # 2 cache keys + 1 reverse key + 1 master set = 4 deletes
        assert pipe.delete.call_count >= 4
        pipe.execute.assert_awaited_once()

    async def test_permission_event_triggers_broad_invalidation(self):
        redis, pipe = _make_redis_mock()
        redis.smembers.return_value = {b"identity:descope:ext-1"}
        redis.scan.return_value = (0, [])

        await _handle_invalidation_event(redis, {"entity_type": "permission", "entity_id": str(uuid.uuid4())})

        redis.smembers.assert_awaited_once_with("identity:all-keys")

    async def test_tenant_event_triggers_broad_invalidation(self):
        redis, pipe = _make_redis_mock()
        redis.smembers.return_value = {b"identity:descope:ext-1"}
        redis.scan.return_value = (0, [])

        await _handle_invalidation_event(redis, {"entity_type": "tenant", "entity_id": str(uuid.uuid4())})

        redis.smembers.assert_awaited_once_with("identity:all-keys")

    async def test_batch_event_triggers_broad_invalidation(self):
        redis, pipe = _make_redis_mock()
        redis.smembers.return_value = {b"identity:descope:ext-1"}
        redis.scan.return_value = (0, [])

        await _handle_invalidation_event(redis, {"entity_type": "batch", "entity_id": "reconciliation"})

        redis.smembers.assert_awaited_once_with("identity:all-keys")

    async def test_broad_invalidation_no_keys_is_noop(self):
        """Broad invalidation with empty master set does nothing."""
        redis, pipe = _make_redis_mock()
        redis.smembers.return_value = set()

        await _handle_invalidation_event(redis, {"entity_type": "role", "entity_id": str(uuid.uuid4())})

        redis.pipeline.assert_not_called()

    async def test_unknown_entity_type_is_noop(self):
        """Unknown entity_type is silently ignored."""
        redis, pipe = _make_redis_mock()

        await _handle_invalidation_event(redis, {"entity_type": "unknown", "entity_id": "x"})

        redis.smembers.assert_not_awaited()
        redis.pipeline.assert_not_called()


@pytest.mark.anyio
class TestRunCacheInvalidationSubscriber:
    """AC-4.3.3: Background subscriber lifecycle."""

    async def test_subscriber_handles_cancellation_gracefully(self):
        """Subscriber shuts down cleanly on task cancellation (no exception propagated)."""
        redis, _ = _make_redis_mock()
        pubsub = _make_pubsub_mock()
        redis.pubsub = MagicMock(return_value=pubsub)

        # Simulate listen() that blocks then gets cancelled
        async def blocking_listen():
            await asyncio.sleep(100)
            yield  # pragma: no cover

        pubsub.listen = MagicMock(return_value=blocking_listen())

        task = asyncio.create_task(run_cache_invalidation_subscriber(redis))
        await asyncio.sleep(0.05)
        task.cancel()
        # Function catches CancelledError gracefully — should complete without error
        await task

        pubsub.subscribe.assert_awaited_once_with("identity:changes")
        pubsub.unsubscribe.assert_awaited_with("identity:changes")

    async def test_subscriber_processes_valid_message(self):
        """Subscriber processes a valid identity:changes message."""
        redis, _ = _make_redis_mock()
        pubsub = _make_pubsub_mock()
        redis.pubsub = MagicMock(return_value=pubsub)
        # Return empty set for smembers (no cache keys to invalidate)
        redis.smembers.return_value = set()

        messages = [
            {"type": "subscribe", "data": None},  # subscription confirmation
            {"type": "message", "data": json.dumps({"entity_type": "user", "entity_id": str(uuid.uuid4())})},
        ]

        async def listen_messages():
            for msg in messages:
                yield msg
            # Simulate end of stream via cancellation (caught gracefully by subscriber)
            raise asyncio.CancelledError

        pubsub.listen = MagicMock(return_value=listen_messages())

        # Function catches CancelledError — completes normally
        await run_cache_invalidation_subscriber(redis)

        # Should have processed the user event (skip subscribe confirmation)
        redis.smembers.assert_awaited_once()

    async def test_subscriber_swallows_handler_exception(self):
        """Bad message data doesn't crash the subscriber loop."""
        redis, _ = _make_redis_mock()
        pubsub = _make_pubsub_mock()
        redis.pubsub = MagicMock(return_value=pubsub)

        messages = [
            {"type": "message", "data": "not-valid-json"},
            {"type": "message", "data": json.dumps({"entity_type": "user", "entity_id": str(uuid.uuid4())})},
        ]

        async def listen_messages():
            for msg in messages:
                yield msg
            raise asyncio.CancelledError

        pubsub.listen = MagicMock(return_value=listen_messages())
        redis.smembers.return_value = set()

        # Function catches CancelledError — completes normally
        await run_cache_invalidation_subscriber(redis)

        # Should still have attempted to process the second message
        # (smembers called for the valid user event)
        redis.smembers.assert_awaited_once()
