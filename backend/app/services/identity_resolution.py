"""IdentityResolutionService — resolve canonical user identity from provider + subject.

AC-4.3.1: Identity resolution endpoint logic.
AC-4.3.2: Redis cache with configurable TTL.
AC-4.3.3: Cache invalidation subscriber on ``identity:changes`` pub/sub channel.

Middle layer of onion architecture: orchestrates repositories for read-only lookups.
All methods return Result[T, IdentityError]. OTel spans on every method.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import IdentityError, NotFound
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

IDENTITY_CACHE_TTL = int(os.getenv("IDENTITY_CACHE_TTL", "300"))


class IdentityResolutionService:
    """Resolve canonical user identity from IdP provider name + external subject.

    Read-only service — no write-through sync. Caches results in Redis when available.
    """

    def __init__(
        self,
        *,
        user_repository: UserRepository,
        idp_link_repository: IdPLinkRepository,
        provider_repository: ProviderRepository,
        assignment_repository: UserTenantRoleRepository,
        role_repository: RoleRepository,
        tenant_repository: TenantRepository,
        redis_client: Redis | None = None,
    ) -> None:
        self._user_repository = user_repository
        self._idp_link_repository = idp_link_repository
        self._provider_repository = provider_repository
        self._assignment_repository = assignment_repository
        self._role_repository = role_repository
        self._tenant_repository = tenant_repository
        self._redis = redis_client

    async def resolve(
        self,
        *,
        provider: str,
        sub: str,
    ) -> Result[dict[str, Any], IdentityError]:
        """Resolve canonical identity for a provider name + external subject.

        Returns user profile, roles with permissions, tenant memberships, and linked IdPs.
        """
        with tracer.start_as_current_span(
            "IdentityResolutionService.resolve",
            attributes={"identity.provider": provider, "identity.sub": sub},
        ):
            # Try cache first (AC-4.3.2)
            cache_key = f"identity:{provider}:{sub}"
            cached = await self._cache_get(cache_key)
            if cached is not None:
                return Ok(cached)

            # Look up provider by name
            provider_entity = await self._provider_repository.get_by_name(provider)
            if provider_entity is None:
                return Error(NotFound(f"Provider '{provider}' not found"))

            # Find IdP link by provider name + external sub
            link = await self._idp_link_repository.get_by_provider_name_and_sub(provider, sub)
            if link is None:
                return Error(NotFound(f"No identity found for provider '{provider}' with sub '{sub}'"))

            # Load the canonical user (should always exist via FK)
            user = await self._user_repository.get(link.user_id)
            if user is None:
                return Error(NotFound("Linked user not found"))

            # Build the full identity payload
            payload = await self._build_identity_payload(user, link.user_id)

            # Cache the result (AC-4.3.2)
            await self._cache_set(cache_key, payload, link.user_id)

            return Ok(payload)

    async def _build_identity_payload(
        self,
        user: Any,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Build the canonical identity payload with roles, permissions, and memberships."""
        # Get all role assignments for this user
        assignments = await self._assignment_repository.list_by_user(user_id)

        # Group assignments by tenant and load role details + permissions
        roles: list[dict[str, Any]] = []
        tenant_ids: set[uuid.UUID] = set()

        for assignment in assignments:
            tenant_ids.add(assignment.tenant_id)
            role = await self._role_repository.get(assignment.role_id)
            if role is None:
                continue
            permissions = await self._role_repository.get_permissions(assignment.role_id)
            roles.append(
                {
                    "tenant_id": str(assignment.tenant_id),
                    "role_name": role.name,
                    "permissions": [p.name for p in permissions],
                }
            )

        # Load tenant details for memberships
        tenant_memberships: list[dict[str, str]] = []
        for tid in tenant_ids:
            tenant = await self._tenant_repository.get(tid)
            if tenant is not None:
                tenant_memberships.append(
                    {
                        "tenant_id": str(tenant.id),
                        "tenant_name": tenant.name,
                    }
                )

        # Load all linked IdPs for this user
        links = await self._idp_link_repository.get_by_user(user_id)
        linked_providers: list[dict[str, str]] = []
        for lnk in links:
            prov = await self._provider_repository.get(lnk.provider_id)
            provider_name = prov.name if prov else "unknown"
            linked_providers.append(
                {
                    "provider_name": provider_name,
                    "external_sub": lnk.external_sub,
                }
            )

        return {
            "user": {
                "id": str(user.id),
                "email": user.email,
                "user_name": user.user_name,
                "given_name": user.given_name,
                "family_name": user.family_name,
                "status": user.status.value if hasattr(user.status, "value") else str(user.status),
            },
            "roles": roles,
            "tenant_memberships": tenant_memberships,
            "linked_idps": linked_providers,
        }

    # ---- Redis cache helpers (AC-4.3.2) ----

    async def _cache_get(self, key: str) -> dict[str, Any] | None:
        """Read from Redis cache. Returns None on miss or Redis failure."""
        if self._redis is None:
            return None
        try:
            data = await self._redis.get(key)
            if data is not None:
                return json.loads(data)
        except Exception:
            logger.warning("Redis cache read failed for key %s", key, exc_info=True)
        return None

    async def _cache_set(self, key: str, payload: dict[str, Any], user_id: uuid.UUID) -> None:
        """Write to Redis cache with TTL. Also updates reverse index and master set."""
        if self._redis is None:
            return
        try:
            serialized = json.dumps(payload)
            pipe = self._redis.pipeline()
            pipe.setex(key, IDENTITY_CACHE_TTL, serialized)
            # Reverse index: identity:user:{user_id} → set of cache keys
            reverse_key = f"identity:user:{user_id}"
            pipe.sadd(reverse_key, key)
            pipe.expire(reverse_key, IDENTITY_CACHE_TTL)
            # Master set for bulk invalidation
            pipe.sadd("identity:all-keys", key)
            await pipe.execute()
        except Exception:
            logger.warning("Redis cache write failed for key %s", key, exc_info=True)


# ---- Cache invalidation subscriber (AC-4.3.3) ----


async def run_cache_invalidation_subscriber(redis_client: Redis) -> None:
    """Subscribe to ``identity:changes`` and invalidate relevant cache entries.

    Runs as a background asyncio task. Graceful shutdown via task cancellation.
    """
    pubsub = redis_client.pubsub()
    try:
        await pubsub.subscribe("identity:changes")
        logger.info("Cache invalidation subscriber started on identity:changes")

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                event = json.loads(message["data"])
                await _handle_invalidation_event(redis_client, event)
            except Exception:
                logger.warning("Cache invalidation handler failed", exc_info=True)
    except asyncio.CancelledError:
        logger.info("Cache invalidation subscriber shutting down")
    except Exception:
        logger.warning("Cache invalidation subscriber error", exc_info=True)
    finally:
        try:
            await pubsub.unsubscribe("identity:changes")
            await pubsub.aclose()
        except Exception:
            logger.warning("Cache invalidation subscriber cleanup failed", exc_info=True)


async def _handle_invalidation_event(redis_client: Redis, event: dict[str, Any]) -> None:
    """Invalidate cache entries based on a change event."""
    entity_type = event.get("entity_type", "")
    entity_id = event.get("entity_id", "")

    if entity_type == "user":
        # Delete all cache entries for this user via reverse index
        reverse_key = f"identity:user:{entity_id}"
        keys = await redis_client.smembers(reverse_key)
        if keys:
            pipe = redis_client.pipeline()
            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                pipe.delete(key_str)
                pipe.srem("identity:all-keys", key_str)
            pipe.delete(reverse_key)
            await pipe.execute()
            logger.debug("Invalidated %d cache entries for user %s", len(keys), entity_id)
    elif entity_type in ("role", "permission", "tenant", "batch"):
        # Broad invalidation: delete all identity cache keys
        all_keys = await redis_client.smembers("identity:all-keys")
        if all_keys:
            pipe = redis_client.pipeline()
            for key in all_keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                pipe.delete(key_str)
                # Also delete any reverse index keys
                if key_str.startswith("identity:user:"):
                    continue
                # Extract user reverse keys from the cache entries is complex,
                # so just delete the master set — TTL will clean up orphaned reverse keys
            pipe.delete("identity:all-keys")
            await pipe.execute()
            logger.debug("Broad invalidation: deleted %d cache entries for %s change", len(all_keys), entity_type)
