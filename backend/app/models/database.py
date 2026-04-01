import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is required. "
        "Set it in .env, docker-compose.yml, or your shell environment. "
        "Example: DATABASE_URL=postgresql+asyncpg://host:5432/dbname"
    )

# Validate that the URL uses an async-compatible driver scheme
_scheme = DATABASE_URL.split("://")[0] if "://" in DATABASE_URL else ""
if not _scheme:
    raise RuntimeError("DATABASE_URL is malformed — expected scheme://... format")
if "+async" not in _scheme and "+aiosqlite" not in _scheme:
    raise RuntimeError(f"DATABASE_URL must use an async driver (e.g. postgresql+asyncpg://...), got scheme: {_scheme}")

async_engine = create_async_engine(DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def get_async_session():
    async with async_session_factory() as session:
        yield session
