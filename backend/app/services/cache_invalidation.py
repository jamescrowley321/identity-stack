"""CacheInvalidationPublisher — fire-and-forget Redis pub/sub for cache invalidation.

Publishes change events to Redis so downstream caches can invalidate stale entries.
All publish operations are best-effort: failures are logged as errors and never
propagate to the caller (AC-3.3.2).

Channel: ``identity:changes``

Event schema (JSON on the wire)::

    {
        "entity_type": "user" | "role" | "permission" | "tenant",
        "entity_id": "<uuid>",
        "operation": "create" | "update" | "delete" | "deactivate"
                    | "activate" | "assign" | "unassign" | "sync" | "reconcile",
        "tenant_id": "<uuid>" | null,
        "timestamp": "<ISO-8601 UTC>"
    }

Key patterns for consumers:
- ``identity:changes`` — single channel for all entity mutations
- ``entity_type`` + ``entity_id`` — identify the affected record
- ``tenant_id`` — scope invalidation to a tenant (null for global entities)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

CHANNEL = "identity:changes"


class CacheInvalidationPublisher:
    """Best-effort Redis pub/sub publisher for identity change events.

    AC-3.3.1: Called after every repo.commit() in write methods.
    AC-3.3.2: All exceptions caught, logged as errors, never raised.
    AC-3.3.3: Schema documented above; channel = ``identity:changes``.
    """

    def __init__(self, redis_client: Redis | None = None) -> None:
        self._redis = redis_client

    async def publish(
        self,
        *,
        entity_type: str,
        entity_id: UUID | str,
        operation: str,
        tenant_id: UUID | str | None = None,
    ) -> None:
        """Publish a single change event. Silent on failure (AC-3.3.2)."""
        if self._redis is None:
            return
        event = self._build_event(entity_type, entity_id, operation, tenant_id)
        try:
            await self._redis.publish(CHANNEL, json.dumps(event))
        except Exception:
            logger.error("Redis publish failed for %s/%s", entity_type, entity_id, exc_info=True)

    async def publish_batch(
        self,
        *,
        operation: str,
        stats: dict[str, Any],
    ) -> None:
        """Publish a single batch event for reconciliation. Silent on failure."""
        if self._redis is None:
            return
        event = self._build_event("batch", "reconciliation", operation, None)
        event["stats"] = stats
        try:
            await self._redis.publish(CHANNEL, json.dumps(event))
        except Exception:
            logger.error("Redis publish failed for batch/%s", operation, exc_info=True)

    @staticmethod
    def _build_event(
        entity_type: str,
        entity_id: UUID | str,
        operation: str,
        tenant_id: UUID | str | None,
    ) -> dict[str, Any]:
        return {
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "operation": operation,
            "tenant_id": str(tenant_id) if tenant_id is not None else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors descope.py pattern)
# ---------------------------------------------------------------------------
_publisher: CacheInvalidationPublisher | None = None
_NOOP_PUBLISHER = CacheInvalidationPublisher()


def get_cache_publisher() -> CacheInvalidationPublisher:
    """Return the singleton CacheInvalidationPublisher.

    Returns a no-op publisher (redis_client=None) if not initialised,
    so callers never need to null-check.
    """
    if _publisher is not None:
        return _publisher
    logger.warning("get_cache_publisher() called before init_cache_publisher() — returning no-op")
    return _NOOP_PUBLISHER


def init_cache_publisher(redis_client: Redis | None = None) -> CacheInvalidationPublisher:
    """Initialise the singleton publisher during app lifespan."""
    global _publisher  # noqa: PLW0603
    if _publisher is not None:
        logger.warning("init_cache_publisher() called twice — replacing existing publisher")
    _publisher = CacheInvalidationPublisher(redis_client=redis_client)
    return _publisher


def shutdown_cache_publisher() -> None:
    """Clear the singleton on shutdown."""
    global _publisher  # noqa: PLW0603
    _publisher = None


# ---------------------------------------------------------------------------
# Redis client singleton (shared between publisher and identity resolution)
# ---------------------------------------------------------------------------
_redis_client: Redis | None = None


def get_redis_client() -> Redis | None:
    """Return the shared Redis client, or None if not initialised."""
    return _redis_client


def set_redis_client(client: Redis | None) -> None:
    """Store the shared Redis client during app lifespan."""
    global _redis_client  # noqa: PLW0603
    _redis_client = client
