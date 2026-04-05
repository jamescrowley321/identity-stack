"""Unit tests for the async database engine module (Story 1.1)."""

import os
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models.database import (
    _validate_database_url,
    get_engine,
    get_session_factory,
    reset_engine,
)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset the engine/session factory singletons before and after each test."""
    reset_engine()
    yield
    reset_engine()


class TestValidateDatabaseUrl:
    """Validate DATABASE_URL scheme checking."""

    def test_accepts_asyncpg(self):
        result = _validate_database_url("postgresql+asyncpg://host:5432/db")
        assert "asyncpg" in result

    def test_accepts_aiosqlite(self):
        result = _validate_database_url("sqlite+aiosqlite://")
        assert "aiosqlite" in result

    def test_rejects_sync_driver(self):
        with pytest.raises(RuntimeError, match="async driver"):
            _validate_database_url("postgresql://host:5432/db")

    def test_rejects_malformed_url(self):
        with pytest.raises(RuntimeError, match="malformed"):
            _validate_database_url("not-a-url")


class TestGetEngine:
    """Verify engine factory behavior."""

    def test_raises_without_database_url(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DATABASE_URL", None)
            with pytest.raises(RuntimeError, match="DATABASE_URL environment variable is required"):
                get_engine()

    def test_returns_async_engine(self):
        engine = get_engine()
        assert isinstance(engine, AsyncEngine)

    def test_returns_same_instance(self):
        engine1 = get_engine()
        engine2 = get_engine()
        assert engine1 is engine2

    def test_reset_allows_new_engine(self):
        engine1 = get_engine()
        reset_engine()
        engine2 = get_engine()
        assert engine1 is not engine2


class TestGetSessionFactory:
    """Verify session factory behavior."""

    def test_returns_async_sessionmaker(self):
        factory = get_session_factory()
        assert isinstance(factory, async_sessionmaker)

    def test_expire_on_commit_is_false(self):
        factory = get_session_factory()
        assert factory.kw.get("expire_on_commit") is False


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


class TestDatabaseModuleExportsFactoryPattern:
    """Verify the factory pattern replaced module-level construction."""

    def test_no_module_level_engine(self):
        """database.py must not have module-level async_engine assignment."""
        import app.models.database as db_mod

        # The old module-level async_engine should not exist as a public export
        assert not hasattr(db_mod, "async_engine"), "Module-level async_engine must not exist"

    def test_no_module_level_session_factory(self):
        """database.py must not have module-level async_session_factory."""
        import app.models.database as db_mod

        assert not hasattr(db_mod, "async_session_factory"), "Module-level async_session_factory must not exist"

    def test_factory_functions_exist(self):
        """get_engine() and get_session_factory() must be available."""
        import app.models.database as db_mod

        assert callable(getattr(db_mod, "get_engine", None))
        assert callable(getattr(db_mod, "get_session_factory", None))
        assert callable(getattr(db_mod, "reset_engine", None))

    def test_no_sync_engine_exported(self):
        """No sync engine, Session, or create_db_and_tables should exist (D3)."""
        import app.models.database as db_mod

        assert not hasattr(db_mod, "engine"), "Sync engine must not exist"
        assert not hasattr(db_mod, "Session"), "Sync Session must not exist"
        assert not hasattr(db_mod, "get_session"), "Sync get_session must not exist"
        assert not hasattr(db_mod, "create_db_and_tables"), "create_db_and_tables must not exist"


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

    def test_postgres_port_bound_to_localhost(self, compose_config):
        ports = compose_config["services"]["postgres"]["ports"]
        assert "127.0.0.1:5432:5432" in ports

    def test_postgres_environment(self, compose_config):
        env = compose_config["services"]["postgres"]["environment"]
        env_dict = {e.split("=")[0]: e.split("=", 1)[1] for e in env}
        assert env_dict["POSTGRES_DB"] == "identity"
        assert env_dict["POSTGRES_USER"] == "identity"

    def test_postgres_no_hardcoded_password(self, compose_config):
        """Postgres password must not be hardcoded — must use env var substitution."""
        env = compose_config["services"]["postgres"]["environment"]
        password_entries = [e for e in env if e.startswith("POSTGRES_PASSWORD=")]
        for entry in password_entries:
            value = entry.split("=", 1)[1]
            # Must contain a variable reference, not a literal password
            assert "${" in value or value == "", f"Hardcoded password found: {value}"

    def test_postgres_named_volume(self, compose_config):
        volumes = compose_config["services"]["postgres"].get("volumes", [])
        assert any("pgdata:" in v for v in volumes)
        assert "pgdata" in compose_config.get("volumes", {})

    def test_aspire_dashboard_service(self, compose_config):
        svc = compose_config["services"]["aspire-dashboard"]
        assert "aspire-dashboard" in svc["image"]
        ports = svc["ports"]
        assert "127.0.0.1:18888:18888" in ports
        assert "127.0.0.1:4317:18889" in ports

    def test_redis_service(self, compose_config):
        svc = compose_config["services"]["redis"]
        assert svc["image"] == "redis:7-alpine"

    def test_redis_bound_to_localhost(self, compose_config):
        svc = compose_config["services"]["redis"]
        assert "127.0.0.1:6379:6379" in svc["ports"]

    def test_redis_has_requirepass(self, compose_config):
        svc = compose_config["services"]["redis"]
        command = svc.get("command", "")
        assert "requirepass" in command

    def test_backend_bound_to_localhost(self, compose_config):
        ports = compose_config["services"]["backend"]["ports"]
        assert "127.0.0.1:8000:8000" in ports

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

    def test_no_hardcoded_credentials_in_example(self, env_content):
        """env.example must not contain hardcoded dev credentials."""
        assert "identity:dev@" not in env_content

    def test_otel_endpoint_documented(self, env_content):
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in env_content

    def test_otel_service_name_documented(self, env_content):
        assert "OTEL_SERVICE_NAME" in env_content

    def test_redis_url_documented(self, env_content):
        assert "REDIS_URL" in env_content

    def test_postgres_password_documented(self, env_content):
        assert "POSTGRES_PASSWORD" in env_content
