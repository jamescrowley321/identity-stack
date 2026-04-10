"""Unit test configuration.

Sets DATABASE_URL before any app modules are imported so the database module
can initialize without raising RuntimeError.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/testdb")

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _init_app_state():
    """Ensure app.state has required DI attributes for all unit tests.

    The real app lifespan sets these on startup; tests need them pre-set
    so route handlers can read request.app.state.* without AttributeError.
    """
    from app.main import app
    from app.services.cache_invalidation import CacheInvalidationPublisher

    app.state.descope_client = AsyncMock()
    app.state.cache_publisher = CacheInvalidationPublisher()
    app.state.redis_client = None
    yield
    app.state.descope_client = None
    app.state.cache_publisher = None
    app.state.redis_client = None
