"""Dependency factory for IdentityService injection into FastAPI routes."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_async_session
from app.services.identity import IdentityService


async def get_identity_service(
    session: AsyncSession = Depends(get_async_session),
) -> IdentityService:
    """Return a configured IdentityService implementation.

    Currently raises NotImplementedError — the concrete PostgresIdentityService
    will be wired in story 2.x. The NoOpSyncAdapter is ready for injection.
    """
    # Story 2.x: return PostgresIdentityService(session=session, adapter=NoOpSyncAdapter())
    raise NotImplementedError(
        "PostgresIdentityService is not yet implemented (story 2.x). "
        "Use NoOpSyncAdapter in tests via dependency override."
    )
