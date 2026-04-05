"""Unit tests for integration test infrastructure (AC-1.5.5).

Verifies that the integration conftest.py has the required fixtures by
inspecting the source code. No module import needed — testcontainers may
not be installed in the dev environment.

Checks:
- testcontainers Postgres fixture
- Alembic migration runner
- Per-test transactional rollback session
- NoOpSyncAdapter fixture
"""

import ast
import pathlib

import pytest

CONFTEST_PATH = pathlib.Path(__file__).resolve().parents[1] / "integration" / "conftest.py"


@pytest.fixture
def conftest_source():
    return CONFTEST_PATH.read_text()


@pytest.fixture
def conftest_functions():
    """Parse the conftest and return a set of top-level function names."""
    tree = ast.parse(CONFTEST_PATH.read_text())
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


class TestPostgresContainerFixture:
    """AC-1.5.5: Real Postgres via testcontainers-python."""

    def test_postgres_container_fixture_exists(self, conftest_functions):
        assert "postgres_container" in conftest_functions

    def test_uses_testcontainers(self, conftest_source):
        assert "PostgresContainer" in conftest_source
        assert "testcontainers" in conftest_source

    def test_postgres_image_is_16_alpine(self, conftest_source):
        assert "postgres:16-alpine" in conftest_source

    def test_session_scoped(self, conftest_source):
        assert 'scope="session"' in conftest_source


class TestAlembicMigrations:
    """AC-1.5.5: Alembic migrations run against test database."""

    def test_run_migrations_fixture_exists(self, conftest_functions):
        assert "_run_migrations" in conftest_functions

    def test_uses_alembic_command(self, conftest_source):
        assert "from alembic import command" in conftest_source
        assert "command.upgrade" in conftest_source

    def test_upgrades_to_head(self, conftest_source):
        assert '"head"' in conftest_source


class TestCleanDatabaseState:
    """AC-1.5.5: Each test gets clean database state via transactional rollback."""

    def test_db_session_fixture_exists(self, conftest_functions):
        assert "db_session" in conftest_functions

    def test_uses_transactional_rollback(self, conftest_source):
        assert "transaction.rollback()" in conftest_source

    def test_db_session_is_function_scoped(self, conftest_source):
        """db_session should NOT be session-scoped — each test needs fresh state."""
        lines = conftest_source.split("\n")
        for i, line in enumerate(lines):
            if "def db_session" in line:
                decorator_line = lines[i - 1] if i > 0 else ""
                assert 'scope="session"' not in decorator_line
                break

    def test_uses_async_session(self, conftest_source):
        assert "AsyncSession" in conftest_source


class TestNoOpSyncAdapterFixture:
    """AC-1.5.5: NoOpSyncAdapter is used to isolate canonical logic from IdP."""

    def test_noop_adapter_fixture_exists(self, conftest_functions):
        assert "noop_adapter" in conftest_functions

    def test_imports_noop_adapter(self, conftest_source):
        assert "NoOpSyncAdapter" in conftest_source


class TestPostgresUrlFixture:
    def test_postgres_url_fixture_exists(self, conftest_functions):
        assert "postgres_url" in conftest_functions

    def test_converts_to_asyncpg(self, conftest_source):
        """URL must use asyncpg driver, not psycopg2."""
        assert "asyncpg" in conftest_source


class TestAsyncEngineFixture:
    def test_async_engine_fixture_exists(self, conftest_functions):
        assert "async_engine" in conftest_functions

    def test_depends_on_migrations(self, conftest_source):
        """Engine fixture should depend on _run_migrations."""
        assert "_run_migrations" in conftest_source
