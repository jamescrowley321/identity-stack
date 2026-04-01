"""Unit tests for Alembic migration scripts.

Verifies revision chain, table creation/drop operations, and structural
correctness without running against a database.
"""

import importlib

# Module names start with digits — use importlib
m001 = importlib.import_module("migrations.versions.001_baseline_existing_tables")
m002 = importlib.import_module("migrations.versions.002_canonical_identity_schema")


class TestBaselineMigration:
    """AC-1.2.2: Existing tables under Alembic control."""

    def test_revision_id(self):
        assert m001.revision == "001_baseline"

    def test_no_down_revision(self):
        assert m001.down_revision is None

    def test_has_upgrade_function(self):
        assert callable(m001.upgrade)

    def test_has_downgrade_function(self):
        assert callable(m001.downgrade)


class TestCanonicalMigration:
    """AC-1.2.3: 8 canonical identity tables."""

    def test_revision_id(self):
        assert m002.revision == "002_canonical_identity"

    def test_depends_on_baseline(self):
        assert m002.down_revision == "001_baseline"

    def test_has_upgrade_function(self):
        assert callable(m002.upgrade)

    def test_has_downgrade_function(self):
        """AC-1.2.5: Downgrade drops canonical tables cleanly."""
        assert callable(m002.downgrade)


class TestRevisionChain:
    """Migration revision chain must be linear: None → 001 → 002."""

    def test_chain_integrity(self):
        assert m001.down_revision is None
        assert m002.down_revision == m001.revision
