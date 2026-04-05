"""Dependency factories for identity domain services (onion architecture DI wiring).

AC-2.1.7: get_user_service() wires AsyncSession → UserRepository → UserService
with DescopeSyncAdapter wrapping the singleton DescopeManagementClient.
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_async_session
from app.repositories.user import UserRepository
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.descope import get_descope_client
from app.services.user import UserService


async def get_user_service(
    session: AsyncSession = Depends(get_async_session),
) -> UserService:
    """Build a UserService with its repository and sync adapter.

    Wiring: AsyncSession → UserRepository(session)
            DescopeManagementClient → DescopeSyncAdapter(client)
            → UserService(repository, adapter)
    """
    repository = UserRepository(session)
    adapter = DescopeSyncAdapter(client=get_descope_client())
    return UserService(repository=repository, adapter=adapter)
