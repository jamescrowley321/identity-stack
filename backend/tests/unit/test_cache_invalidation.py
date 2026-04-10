"""Unit tests for CacheInvalidationPublisher (Story 3.3).

Tests cover:
- publish: publishes JSON event to identity:changes channel (AC-3.3.1)
- publish: no-op when redis client is None
- publish: swallows Redis exceptions, logs error (AC-3.3.2)
- publish_batch: publishes batch event with stats
- publish_batch: swallows exceptions like publish
- Singleton lifecycle: init, get, shutdown
- Event schema correctness (AC-3.3.3)
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.cache_invalidation import (
    CHANNEL,
    CacheInvalidationPublisher,
    get_cache_publisher,
    init_cache_publisher,
    shutdown_cache_publisher,
)


@pytest.mark.anyio
class TestPublish:
    """AC-3.3.1: publish sends correct event to identity:changes channel."""

    async def test_publish_sends_event_to_channel(self):
        redis = AsyncMock()
        redis.publish.return_value = 1
        pub = CacheInvalidationPublisher(redis_client=redis)
        entity_id = uuid.uuid4()
        tenant_id = uuid.uuid4()

        await pub.publish(
            entity_type="user",
            entity_id=entity_id,
            operation="create",
            tenant_id=tenant_id,
        )

        redis.publish.assert_awaited_once()
        call_args = redis.publish.call_args
        assert call_args[0][0] == CHANNEL
        event = json.loads(call_args[0][1])
        assert event["entity_type"] == "user"
        assert event["entity_id"] == str(entity_id)
        assert event["operation"] == "create"
        assert event["tenant_id"] == str(tenant_id)
        assert "timestamp" in event

    async def test_publish_noop_when_no_redis_client(self):
        """No-op publisher does not raise or attempt to publish."""
        pub = CacheInvalidationPublisher(redis_client=None)
        # Should complete silently — no exception
        await pub.publish(entity_type="user", entity_id=uuid.uuid4(), operation="create")

    async def test_publish_swallows_redis_exception(self):
        """AC-3.3.2: Redis failure logged as error, never raised."""
        redis = AsyncMock()
        redis.publish.side_effect = ConnectionError("Redis gone")
        pub = CacheInvalidationPublisher(redis_client=redis)

        with patch("app.services.cache_invalidation.logger") as mock_logger:
            await pub.publish(entity_type="user", entity_id=uuid.uuid4(), operation="update")

        mock_logger.error.assert_called_once()
        assert "Redis publish failed" in mock_logger.error.call_args[0][0]

    async def test_publish_null_tenant_id(self):
        """Global entities (roles, permissions) have tenant_id=null."""
        redis = AsyncMock()
        redis.publish.return_value = 1
        pub = CacheInvalidationPublisher(redis_client=redis)

        await pub.publish(entity_type="permission", entity_id=uuid.uuid4(), operation="create")

        event = json.loads(redis.publish.call_args[0][1])
        assert event["tenant_id"] is None


@pytest.mark.anyio
class TestPublishBatch:
    """publish_batch sends a single batch event for reconciliation."""

    async def test_publish_batch_sends_event(self):
        redis = AsyncMock()
        redis.publish.return_value = 1
        pub = CacheInvalidationPublisher(redis_client=redis)
        stats = {"users_created": 3, "roles_updated": 1}

        await pub.publish_batch(operation="reconcile", stats=stats)

        redis.publish.assert_awaited_once()
        event = json.loads(redis.publish.call_args[0][1])
        assert event["entity_type"] == "batch"
        assert event["entity_id"] == "reconciliation"
        assert event["operation"] == "reconcile"
        assert event["stats"] == stats
        assert event["tenant_id"] is None

    async def test_publish_batch_noop_when_no_redis(self):
        pub = CacheInvalidationPublisher(redis_client=None)
        await pub.publish_batch(operation="reconcile", stats={"users_created": 1})

    async def test_publish_batch_swallows_exception(self):
        redis = AsyncMock()
        redis.publish.side_effect = ConnectionError("Redis gone")
        pub = CacheInvalidationPublisher(redis_client=redis)

        with patch("app.services.cache_invalidation.logger") as mock_logger:
            await pub.publish_batch(operation="reconcile", stats={})

        mock_logger.error.assert_called_once()


class TestSingletonLifecycle:
    """Module-level singleton functions: init, get, shutdown."""

    def setup_method(self):
        shutdown_cache_publisher()

    def teardown_method(self):
        shutdown_cache_publisher()

    def test_get_returns_noop_before_init(self):
        pub = get_cache_publisher()
        assert pub._redis is None

    def test_init_and_get_returns_same_instance(self):
        redis = AsyncMock()
        created = init_cache_publisher(redis_client=redis)
        retrieved = get_cache_publisher()
        assert retrieved is created
        assert retrieved._redis is redis

    def test_shutdown_clears_singleton(self):
        init_cache_publisher(redis_client=AsyncMock())
        shutdown_cache_publisher()
        pub = get_cache_publisher()
        assert pub._redis is None
