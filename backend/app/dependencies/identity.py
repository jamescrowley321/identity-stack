"""Dependency factory for IdentityService injection into FastAPI routes."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_async_session
from app.services.adapters.noop import NoOpSyncAdapter
from app.services.identity import IdentityService


async def get_identity_service(
    session: AsyncSession = Depends(get_async_session),
) -> IdentityService:
    """Return a configured IdentityService implementation.

    Currently raises NotImplementedError — the concrete PostgresIdentityService
    will be wired in story 2.x. The NoOpSyncAdapter is ready for injection.
    """
    _adapter = NoOpSyncAdapter()
    # PostgresIdentityService(session=session, adapter=_adapter) — story 2.x
    raise NotImplementedError(
        "PostgresIdentityService is not yet implemented (story 2.x). "
        "Use NoOpSyncAdapter in tests via dependency override."
    )
