import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_logger = logging.getLogger(__name__)

_DEFAULT_DATABASE_URL = "postgresql+asyncpg://identity:dev@postgres:5432/identity"
DATABASE_URL = os.getenv("DATABASE_URL", "")

if not DATABASE_URL:
    _logger.warning(
        "DATABASE_URL is not set — falling back to development default. "
        "This MUST be set explicitly in production deployments."
    )
    DATABASE_URL = _DEFAULT_DATABASE_URL

async_engine = create_async_engine(DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def get_async_session():
    async with async_session_factory() as session:
        yield session
