## Review: Edge Case Hunter

### Findings

| Location | Trigger Condition | Guard Snippet | Consequence |
|----------|-------------------|---------------|-------------|
| `backend/app/services/inbound_sync.py:306` | `_handle_user_deleted` webhook deactivates the user but publishes `operation="sync"` instead of `"deactivate"`; the documented schema lists `"deactivate"` as a distinct operation | Change to `operation="deactivate"` | [WRONG] Downstream cache consumers dispatching on operation type see a sync event, not a deactivation; deactivated users may not be invalidated correctly |
| `backend/app/main.py:87-89` | `shutdown_cache_publisher()` and `redis_client.aclose()` share one `try` block; if `shutdown_cache_publisher()` raises before `aclose()` is reached, the Redis connection is never closed | Split into two sequential `try/finally` blocks so `aclose()` always runs | [DEGRADED] Redis connection leaks on shutdown if `shutdown_cache_publisher` raises (currently cannot raise, but is a structural fragility) |
| `backend/app/services/cache_invalidation.py:114-122` | `get_cache_publisher()` called before `init_cache_publisher()` (e.g., after `shutdown_cache_publisher()` sets `_publisher=None` mid-flight) returns a fresh throwaway no-op instance per call rather than the singleton | Assign the fallback to `_publisher`, or document that only the lifespan path is supported | [DEGRADED] Each pre-init call returns a distinct throwaway object; services injected at DI time hold a different no-op instance than any future call; behaviorally safe but identity is inconsistent and can mask misconfiguration in tests |

### Summary
- Unhandled paths found: 3
- Critical (crash/data loss): 0
- Non-critical (wrong result/degraded): 3
