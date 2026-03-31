"""Unit tests for Alembic configuration and migration structure (Story 1.2).

AC-1.2.1: Alembic async setup
AC-1.2.2: Baseline migration structure
AC-1.2.3: Canonical schema migration structure
AC-1.2.5: Downgrade support
"""

import ast
import configparser
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = BACKEND_DIR / "migrations"
VERSIONS_DIR = MIGRATIONS_DIR / "versions"


class TestAlembicIni:
    """AC-1.2.1: alembic.ini points to the correct location and DATABASE_URL."""

    @pytest.fixture(autouse=True)
    def _load_config(self):
        self.config = configparser.ConfigParser()
        self.config.read(BACKEND_DIR / "alembic.ini")

    def test_script_location_points_to_migrations(self):
        assert self.config.get("alembic", "script_location") == "migrations"

    def test_default_url_uses_asyncpg(self):
        url = self.config.get("alembic", "sqlalchemy.url")
        assert "postgresql+asyncpg://" in url

    def test_default_url_not_sqlite(self):
        url = self.config.get("alembic", "sqlalchemy.url")
        assert "sqlite" not in url


class TestEnvPyAsyncOnly:
    """AC-1.2.1: env.py uses async-only configuration, no sync engine."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        self.source = (MIGRATIONS_DIR / "env.py").read_text()

    def test_imports_create_async_engine(self):
        assert "create_async_engine" in self.source

    def test_no_sync_create_engine(self):
        """No bare create_engine import (only create_async_engine)."""
        for line in self.source.splitlines():
            if "create_engine" in line and "create_async_engine" not in line:
                pytest.fail(f"Sync create_engine found: {line.strip()}")

    def test_uses_asyncio_run(self):
        """Online mode runs migrations via asyncio.run()."""
        assert "asyncio.run" in self.source

    def test_no_sync_session(self):
        assert "from sqlalchemy.orm import Session" not in self.source
        assert "from sqlmodel import Session" not in self.source

    def test_env_imports_all_identity_models(self):
        """env.py imports all identity models so metadata is populated."""
        expected = [
            "User",
            "IdPLink",
            "Tenant",
            "Role",
            "Permission",
            "RolePermission",
            "UserTenantRole",
            "Provider",
        ]
        for model in expected:
            assert model in self.source, f"Missing import for {model}"

    def test_env_imports_existing_models(self):
        """env.py imports existing Document and TenantResource models."""
        assert "Document" in self.source
        assert "TenantResource" in self.source

    def test_target_metadata_is_sqlmodel(self):
        assert "target_metadata = SQLModel.metadata" in self.source

    def test_get_url_reads_database_url_env(self):
        """get_url() reads DATABASE_URL from environment."""
        assert "DATABASE_URL" in self.source


class TestMigrationRevisionChain:
    """AC-1.2.2 / AC-1.2.3: Migration revision chain is correct."""

    def _parse_module_attrs(self, path: Path) -> dict:
        """Extract module-level string assignments from a migration file."""
        tree = ast.parse(path.read_text())
        attrs = {}
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
                if isinstance(node.value, ast.Constant):
                    attrs[name] = node.value.value
        return attrs

    def test_baseline_revision_is_0001(self):
        attrs = self._parse_module_attrs(VERSIONS_DIR / "0001_baseline_existing_tables.py")
        assert attrs["revision"] == "0001"

    def test_baseline_has_no_down_revision(self):
        attrs = self._parse_module_attrs(VERSIONS_DIR / "0001_baseline_existing_tables.py")
        assert attrs["down_revision"] is None

    def test_canonical_revision_is_0002(self):
        attrs = self._parse_module_attrs(VERSIONS_DIR / "0002_canonical_identity_tables.py")
        assert attrs["revision"] == "0002"

    def test_canonical_depends_on_0001(self):
        attrs = self._parse_module_attrs(VERSIONS_DIR / "0002_canonical_identity_tables.py")
        assert attrs["down_revision"] == "0001"


class TestBaselineMigrationStructure:
    """AC-1.2.2: Baseline migration creates documents and tenant_resources."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        self.source = (VERSIONS_DIR / "0001_baseline_existing_tables.py").read_text()

    def test_upgrade_creates_documents_table(self):
        assert '"documents"' in self.source

    def test_upgrade_creates_tenant_resources_table(self):
        assert '"tenant_resources"' in self.source

    def test_upgrade_indexes_documents_tenant_id(self):
        assert "ix_documents_tenant_id" in self.source

    def test_upgrade_indexes_tenant_resources_tenant_id(self):
        assert "ix_tenant_resources_tenant_id" in self.source

    def test_downgrade_drops_both_tables(self):
        source_after_downgrade = self.source.split("def downgrade")[1]
        assert "drop_table" in source_after_downgrade
        assert '"documents"' in source_after_downgrade
        assert '"tenant_resources"' in source_after_downgrade


class TestCanonicalMigrationStructure:
    """AC-1.2.3: Canonical migration creates 8 identity tables."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        self.source = (VERSIONS_DIR / "0002_canonical_identity_tables.py").read_text()

    def test_creates_all_eight_tables(self):
        tables = [
            "users",
            "tenants",
            "roles",
            "permissions",
            "role_permissions",
            "user_tenant_roles",
            "idp_links",
            "providers",
        ]
        for table in tables:
            assert f'"{table}"' in self.source, f"Missing create_table for {table}"

    def test_uses_uuid_primary_keys(self):
        assert "sa.Uuid()" in self.source

    def test_index_users_email(self):
        assert "ix_users_email" in self.source

    def test_index_idp_links_external_sub(self):
        assert "ix_idp_links_external_sub" in self.source

    def test_index_user_tenant_roles_user_tenant(self):
        assert "ix_user_tenant_roles_user_tenant" in self.source

    def test_unique_constraint_users_email(self):
        assert "uq_users_email" in self.source

    def test_unique_constraint_idp_links_user_provider(self):
        assert "uq_idp_links_user_provider" in self.source

    def test_unique_constraint_roles_name_tenant(self):
        assert "uq_roles_name_tenant" in self.source

    def test_foreign_keys_defined(self):
        """FK references exist for cross-table relationships."""
        fk_targets = [
            '"tenants.id"',
            '"users.id"',
            '"roles.id"',
            '"permissions.id"',
            '"providers.id"',
        ]
        for fk in fk_targets:
            assert fk in self.source, f"Missing FK to {fk}"

    def test_uses_jsonb_for_idp_link_metadata(self):
        assert "JSONB" in self.source

    def test_uses_array_for_domains_and_capabilities(self):
        assert "ARRAY" in self.source


class TestCanonicalMigrationDowngrade:
    """AC-1.2.5: Downgrade drops all 8 tables in correct FK order."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        full = (VERSIONS_DIR / "0002_canonical_identity_tables.py").read_text()
        self.downgrade_src = full.split("def downgrade")[1]

    def test_downgrade_drops_all_eight_tables(self):
        tables = [
            "users",
            "tenants",
            "roles",
            "permissions",
            "role_permissions",
            "user_tenant_roles",
            "idp_links",
            "providers",
        ]
        for table in tables:
            assert f'"{table}"' in self.downgrade_src, f"Downgrade missing drop for {table}"

    def test_downgrade_drops_junction_tables_before_entity_tables(self):
        """user_tenant_roles and idp_links must drop before users/tenants/roles."""
        drop_positions = {}
        for line_num, line in enumerate(self.downgrade_src.splitlines()):
            if "drop_table" in line:
                for table in ["users", "tenants", "roles", "user_tenant_roles", "idp_links", "role_permissions"]:
                    if f'"{table}"' in line:
                        drop_positions[table] = line_num

        # Junction tables must appear before their parent tables
        assert drop_positions["user_tenant_roles"] < drop_positions["users"]
        assert drop_positions["user_tenant_roles"] < drop_positions["tenants"]
        assert drop_positions["user_tenant_roles"] < drop_positions["roles"]
        assert drop_positions["idp_links"] < drop_positions["users"]
        assert drop_positions["role_permissions"] < drop_positions["roles"]

    def test_downgrade_drops_indexes(self):
        assert "drop_index" in self.downgrade_src
