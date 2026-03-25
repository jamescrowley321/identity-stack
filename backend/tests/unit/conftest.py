"""Shared fixtures for unit tests."""

import pytest

from app.models.database import create_db_and_tables


@pytest.fixture(autouse=True, scope="session")
def _create_tables():
    """Ensure all database tables exist before tests run."""
    create_db_and_tables()
