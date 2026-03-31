"""Unit tests for the async database module (Story 1.1)."""

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import Field, SQLModel


class _TestModel(SQLModel, table=True):
    """Ephemeral model for database tests."""

    __tablename__ = "test_database_model"

    id: int | None = Field(default=None, primary_key=True)
    name: str


@pytest.fixture
async def _test_engine():
    """Create an in-memory async SQLite engine for testing."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


class TestDatabaseModule:
    """Tests for database.py module-level objects."""

    def test_default_database_url(self):
        """DATABASE_URL defaults to asyncpg Postgres when unset."""
        with patch.dict("os.environ", {}, clear=False):
            import inspect

            import app.models.database as db_mod

            source = inspect.getsource(db_mod)
            assert "postgresql+asyncpg://" in source

    def test_engine_is_async(self):
        """Module-level engine is an AsyncEngine."""
        from app.models.database import engine

        assert isinstance(engine, AsyncEngine)

    def test_session_factory_is_async(self):
        """Module-level session factory produces AsyncSession instances."""
        from app.models.database import async_session_factory

        assert isinstance(async_session_factory, async_sessionmaker)

    def test_session_factory_expire_on_commit_false(self):
        """Session factory has expire_on_commit=False (required for async patterns)."""
        from app.models.database import async_session_factory

        assert async_session_factory.kw.get("expire_on_commit") is False


class TestGetSession:
    """Tests for the get_session async generator dependency."""

    async def test_get_session_yields_async_session(self, _test_engine):
        """get_session yields an AsyncSession instance."""
        factory = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)

        with patch("app.models.database.async_session_factory", factory):
            from app.models.database import get_session

            gen = get_session()
            session = await gen.__anext__()
            try:
                assert isinstance(session, AsyncSession)
            finally:
                try:
                    await gen.__anext__()
                except StopAsyncIteration:
                    pass

    async def test_get_session_session_is_usable(self, _test_engine):
        """Session from get_session can execute queries."""
        factory = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)

        with patch("app.models.database.async_session_factory", factory):
            from app.models.database import get_session

            gen = get_session()
            session = await gen.__anext__()
            try:
                # Insert a row
                row = _TestModel(name="test-row")
                session.add(row)
                await session.commit()

                # Query it back
                from sqlalchemy import select

                result = await session.execute(select(_TestModel))
                rows = result.scalars().all()
                assert len(rows) == 1
                assert rows[0].name == "test-row"
            finally:
                try:
                    await gen.__anext__()
                except StopAsyncIteration:
                    pass

    async def test_get_session_closes_after_use(self, _test_engine):
        """Session is closed after the generator completes."""
        factory = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)

        with patch("app.models.database.async_session_factory", factory):
            from app.models.database import get_session

            gen = get_session()
            session = await gen.__anext__()
            # Exhaust the generator
            try:
                await gen.__anext__()
            except StopAsyncIteration:
                pass

            # Session should not have a pending transaction after cleanup
            assert not session.in_transaction()

    async def test_get_session_multiple_calls_independent(self, _test_engine):
        """Each call to get_session yields an independent session."""
        factory = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)

        with patch("app.models.database.async_session_factory", factory):
            from app.models.database import get_session

            gen1 = get_session()
            gen2 = get_session()
            session1 = await gen1.__anext__()
            session2 = await gen2.__anext__()
            try:
                assert session1 is not session2
            finally:
                for gen in (gen1, gen2):
                    try:
                        await gen.__anext__()
                    except StopAsyncIteration:
                        pass


class TestNoSyncPatterns:
    """Verify the codebase has no sync database patterns (enforcement rule D3)."""

    def test_no_sync_session_import_in_database_module(self):
        """database.py does not import sync Session."""
        import inspect

        import app.models.database as db_mod

        source = inspect.getsource(db_mod)
        # Should not have sync imports
        assert "from sqlmodel import Session" not in source
        assert "from sqlalchemy.orm import Session" not in source

    def test_no_sync_engine_in_database_module(self):
        """database.py uses create_async_engine, not create_engine."""
        import inspect

        import app.models.database as db_mod

        source = inspect.getsource(db_mod)
        assert "create_async_engine" in source
        # Should not have the sync version (but "create_async_engine" contains "create_engine")
        # so check that "create_engine" only appears as part of "create_async_engine"
        lines = source.split("\n")
        for line in lines:
            if "create_engine" in line and "create_async_engine" not in line:
                pytest.fail(f"Sync create_engine found: {line.strip()}")

    def test_no_create_db_and_tables(self):
        """create_db_and_tables() has been removed (Alembic handles schema)."""
        import inspect

        import app.models.database as db_mod

        source = inspect.getsource(db_mod)
        assert "create_db_and_tables" not in source

    def test_no_sqlite_default(self):
        """Default DATABASE_URL is not SQLite."""
        import inspect

        import app.models.database as db_mod

        source = inspect.getsource(db_mod)
        assert "sqlite" not in source
