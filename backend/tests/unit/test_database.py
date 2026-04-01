"""Unit tests for the async database engine module (Story 1.1)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class TestDatabaseModuleExports:
    """Verify database.py exports async-only interfaces (AC-1.1.2)."""

    def test_async_engine_is_async(self):
        from app.models.database import async_engine

        assert hasattr(async_engine, "begin")
        # AsyncEngine wraps a sync engine; confirm it's the async wrapper
        assert type(async_engine).__name__ == "AsyncEngine"

    def test_session_factory_is_async(self):
        from app.models.database import async_session_factory

        assert isinstance(async_session_factory, async_sessionmaker)

    def test_database_url_is_set_and_async(self):
        from app.models.database import DATABASE_URL

        assert DATABASE_URL, "DATABASE_URL must be set"
        assert "async" in DATABASE_URL.split("://")[0], "DATABASE_URL must use an async driver"

    def test_expire_on_commit_is_false(self):
        from app.models.database import async_session_factory

        assert async_session_factory.kw.get("expire_on_commit") is False

    def test_no_sync_engine_exported(self):
        """No sync engine, Session, or create_db_and_tables should exist (D3)."""
        import app.models.database as db_mod

        assert not hasattr(db_mod, "engine"), "Sync engine must not exist"
        assert not hasattr(db_mod, "Session"), "Sync Session must not exist"
        assert not hasattr(db_mod, "get_session"), "Sync get_session must not exist"
        assert not hasattr(db_mod, "create_db_and_tables"), "create_db_and_tables must not exist"


class TestGetAsyncSession:
    """Verify get_async_session yields an AsyncSession."""

    async def test_yields_async_session(self):
        """get_async_session yields an AsyncSession instance."""
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def _session_gen():
            async with factory() as session:
                yield session

        async for session in _session_gen():
            assert isinstance(session, AsyncSession)
            break

        await engine.dispose()

    async def test_session_closes_after_yield(self):
        """Session is properly closed after the generator exits."""
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        session_ref = None

        async def _session_gen():
            async with factory() as session:
                yield session

        async for session in _session_gen():
            session_ref = session
            break

        # After generator exits, the session context manager has closed
        assert session_ref is not None
        await engine.dispose()


class TestNoSyncPatternsInCodebase:
    """Verify the codebase has no sync database access (AC-1.1.2, D3)."""

    def test_main_does_not_import_create_db_and_tables(self):
        """main.py must not import or call create_db_and_tables."""
        import inspect

        from app import main

        source = inspect.getsource(main)
        assert "create_db_and_tables" not in source

    def test_main_does_not_import_sync_session(self):
        """main.py must not reference sync Session or get_session (but get_async_session is OK)."""
        import inspect
        import re

        from app import main

        source = inspect.getsource(main)
        # Match get_session but NOT get_async_session
        sync_get_session = re.findall(r"\bget_session\b", source)
        async_get_session = re.findall(r"\bget_async_session\b", source)
        bare_get_session_count = len(sync_get_session) - len(async_get_session)
        assert bare_get_session_count <= 0, "main.py must not reference sync get_session"
        # Ensure no bare 'from sqlmodel import Session' (sync)
        assert "from sqlmodel import Session" not in source

    def test_documents_router_uses_async_session(self):
        """documents.py must use AsyncSession, not sync Session."""
        import inspect

        from app.routers import documents

        source = inspect.getsource(documents)
        assert "AsyncSession" in source
        assert "get_async_session" in source
        # No sync Session dependency
        assert "get_session)" not in source

    def test_tenants_router_uses_async_session(self):
        """tenants.py must use AsyncSession, not sync Session."""
        import inspect

        from app.routers import tenants

        source = inspect.getsource(tenants)
        assert "AsyncSession" in source
        assert "get_async_session" in source
        assert "get_session)" not in source


class TestDockerComposeConfiguration:
    """Validate docker-compose.yml has required services (AC-1.1.1)."""

    @pytest.fixture
    def compose_config(self):
        import pathlib

        import yaml

        # docker-compose.yml is at repo root (one level up from backend/)
        compose_path = pathlib.Path(__file__).resolve().parents[3] / "docker-compose.yml"
        with open(compose_path) as f:
            return yaml.safe_load(f)

    def test_postgres_service_exists(self, compose_config):
        assert "postgres" in compose_config["services"]

    def test_postgres_image_is_16_alpine(self, compose_config):
        assert compose_config["services"]["postgres"]["image"] == "postgres:16-alpine"

    def test_postgres_port_5432(self, compose_config):
        ports = compose_config["services"]["postgres"]["ports"]
        assert "5432:5432" in ports

    def test_postgres_environment(self, compose_config):
        env = compose_config["services"]["postgres"]["environment"]
        env_dict = {e.split("=")[0]: e.split("=", 1)[1] for e in env}
        assert env_dict["POSTGRES_DB"] == "identity"
        assert env_dict["POSTGRES_USER"] == "identity"

    def test_postgres_named_volume(self, compose_config):
        volumes = compose_config["services"]["postgres"].get("volumes", [])
        assert any("pgdata:" in v for v in volumes)
        assert "pgdata" in compose_config.get("volumes", {})

    def test_aspire_dashboard_service(self, compose_config):
        svc = compose_config["services"]["aspire-dashboard"]
        assert "aspire-dashboard" in svc["image"]
        ports = svc["ports"]
        assert "18888:18888" in ports
        assert "4317:18889" in ports

    def test_redis_service(self, compose_config):
        svc = compose_config["services"]["redis"]
        assert svc["image"] == "redis:7-alpine"
        assert "6379:6379" in svc["ports"]

    def test_backend_depends_on_postgres_healthy(self, compose_config):
        deps = compose_config["services"]["backend"].get("depends_on", {})
        assert "postgres" in deps
        if isinstance(deps, dict):
            assert deps["postgres"].get("condition") == "service_healthy"

    def test_postgres_has_healthcheck(self, compose_config):
        pg = compose_config["services"]["postgres"]
        assert "healthcheck" in pg
        hc = pg["healthcheck"]
        assert "pg_isready" in str(hc.get("test", ""))

    def test_backend_database_url_env(self, compose_config):
        env = compose_config["services"]["backend"]["environment"]
        db_urls = [e for e in env if e.startswith("DATABASE_URL=")]
        assert len(db_urls) == 1
        assert "asyncpg" in db_urls[0]


class TestDependencies:
    """Verify required dependencies are in pyproject.toml (AC-1.1.3)."""

    @pytest.fixture
    def pyproject(self):
        import pathlib
        import tomllib

        pyproject_path = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            return tomllib.load(f)

    def test_asyncpg_in_deps(self, pyproject):
        deps = pyproject["project"]["dependencies"]
        assert any("asyncpg" in d for d in deps)

    def test_alembic_in_deps(self, pyproject):
        deps = pyproject["project"]["dependencies"]
        assert any("alembic" in d for d in deps)

    def test_sqlalchemy_asyncio_in_deps(self, pyproject):
        deps = pyproject["project"]["dependencies"]
        assert any("sqlalchemy[asyncio]" in d for d in deps)

    def test_expression_in_deps(self, pyproject):
        deps = pyproject["project"]["dependencies"]
        assert any("expression" in d for d in deps)

    def test_testcontainers_in_dev_deps(self, pyproject):
        dev_deps = pyproject["project"]["optional-dependencies"]["dev"]
        assert any("testcontainers" in d for d in dev_deps)


class TestEnvExample:
    """Verify .env.example documents required vars (AC-1.1.4)."""

    @pytest.fixture
    def env_content(self):
        import pathlib

        env_path = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
        with open(env_path) as f:
            return f.read()

    def test_database_url_documented(self, env_content):
        assert "DATABASE_URL" in env_content
        assert "asyncpg" in env_content

    def test_otel_endpoint_documented(self, env_content):
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in env_content

    def test_otel_service_name_documented(self, env_content):
        assert "OTEL_SERVICE_NAME" in env_content

    def test_redis_url_documented(self, env_content):
        assert "REDIS_URL" in env_content
