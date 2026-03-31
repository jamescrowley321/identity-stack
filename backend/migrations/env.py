import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

# Add backend directory to Python path for model imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.document import Document  # noqa: E402, F401
from app.models.identity.assignment import UserTenantRole  # noqa: E402, F401
from app.models.identity.provider import Provider  # noqa: E402, F401
from app.models.identity.role import Permission, Role, RolePermission  # noqa: E402, F401
from app.models.identity.tenant import Tenant  # noqa: E402, F401
from app.models.identity.user import IdPLink, User  # noqa: E402, F401
from app.models.tenant import TenantResource  # noqa: E402, F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def get_url() -> str:
    return os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — generates SQL script."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using async engine — no sync engine anywhere."""
    connectable = create_async_engine(get_url())
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
