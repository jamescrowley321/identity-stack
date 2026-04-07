## Review: Edge Case Hunter

### Findings

| Location | Trigger Condition | Guard Snippet | Consequence |
|----------|-------------------|---------------|-------------|
| `backend/app/services/inbound_sync.py:256` | `_handle_user_deleted` update raises `RepositoryConflictError` | `try: await self._user_repo.update(user) except RepositoryConflictError: return Error(Conflict(...))` | [CRASH] Unhandled exception propagates as 500 to caller |
| `backend/app/repositories/provider.py:30` | Two providers with `type=descope` exist in DB | `return result.scalars().first()` or add unique constraint on `type` | [CRASH] `MultipleResultsFound` raised by `scalar_one_or_none()` |

### Summary
- Unhandled paths found: 2
- Critical (crash/data loss): 2
- Non-critical (wrong result/degraded): 0

#### Notes on Findings

**Finding 1 (`_handle_user_deleted` unguarded update -- CRASH):** In `_handle_user_deleted` (line 256), `await self._user_repo.update(user)` is called without a try/except for `RepositoryConflictError`. The sibling method `_handle_user_updated` (line 222-224) wraps the identical `update()` call in a try/except. `UserRepository.update()` calls `session.flush()` which can raise `IntegrityError` converted to `RepositoryConflictError`. While setting `status=inactive` alone is unlikely to violate a unique constraint on the current schema, the `flush()` call flushes ALL pending changes in the session, not just the status change. If any other pending mutation on the user (or related entity in the same session) triggers an integrity violation, the exception propagates unhandled, producing a 500.

**Finding 2 (`ProviderRepository.get_by_type` with duplicate providers -- CRASH):** `ProviderRepository.get_by_type()` (line 30) uses `scalar_one_or_none()`, which raises `sqlalchemy.exc.MultipleResultsFound` if the query returns more than one row. The `Provider.type` column has `index=True` but NOT `unique=True` (see `provider.py:27`), so the schema permits multiple providers of the same type. If two `descope`-type providers are inserted, every call to `sync_user_from_flow`, `_handle_user_updated`, and `_handle_user_deleted` will crash with an unhandled `MultipleResultsFound`. None of the calling code catches this exception.
