"""Dependency factory for IdentityService injection into FastAPI routes."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_async_session
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.descope import get_descope_client
from app.services.identity import IdentityService
from app.services.identity_impl import PostgresIdentityService


async def get_identity_service(
    session: AsyncSession = Depends(get_async_session),
) -> IdentityService:
    """Return a configured IdentityService with Descope sync adapter."""
    adapter = DescopeSyncAdapter(get_descope_client())
    return PostgresIdentityService(session=session, adapter=adapter)
