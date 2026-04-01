import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is required. "
        "Set it in .env, docker-compose.yml, or your shell environment. "
        "Example: DATABASE_URL=postgresql+asyncpg://identity:dev@localhost:5432/identity"
    )

if "+" in DATABASE_URL.split("://")[0] and "async" not in DATABASE_URL.split("://")[0]:
    raise RuntimeError(
        f"DATABASE_URL must use an async driver (e.g. postgresql+asyncpg://...), got: {DATABASE_URL.split('://')[0]}"
    )

async_engine = create_async_engine(DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def get_async_session():
    async with async_session_factory() as session:
        yield session
