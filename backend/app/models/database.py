"""Async database engine and session factory.

Uses a factory pattern so the engine is not constructed at import time.
This allows tests to override DATABASE_URL and avoids import-time failures in CI.
"""

import os

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def _validate_database_url(url: str) -> str:
    """Validate that a DATABASE_URL is well-formed and uses an async driver.

    Raises:
        RuntimeError: If the URL is missing, malformed, or uses a sync driver.
    """
    scheme = url.split("://")[0] if "://" in url else ""
    if not scheme:
        raise RuntimeError("DATABASE_URL is malformed — expected scheme://... format")
    if "+async" not in scheme and "+aiosqlite" not in scheme:
        raise RuntimeError(
            f"DATABASE_URL must use an async driver (e.g. postgresql+asyncpg://...), got scheme: {scheme}"
        )
    return url


def get_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it on first call.

    Raises:
        RuntimeError: If DATABASE_URL is not set or is invalid.
    """
    global _async_engine  # noqa: PLW0603
    if _async_engine is None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL environment variable is required. "
                "Set it in .env, docker-compose.yml, or your shell environment. "
                "Example: DATABASE_URL=postgresql+asyncpg://host:5432/dbname"
            )
        _validate_database_url(database_url)
        _async_engine = create_async_engine(database_url, echo=False)
    return _async_engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the singleton async session factory, creating it on first call."""
    global _async_session_factory  # noqa: PLW0603
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(get_engine(), class_=AsyncSession, expire_on_commit=False)
    return _async_session_factory


async def get_async_session():
    """FastAPI dependency that yields an AsyncSession."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


def reset_engine() -> None:
    """Reset the singleton engine and session factory. Used in tests only."""
    global _async_engine, _async_session_factory  # noqa: PLW0603
    _async_engine = None
    _async_session_factory = None
