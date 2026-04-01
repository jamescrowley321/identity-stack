"""Unit test configuration.

Sets DATABASE_URL before any app modules are imported so the database module
can initialize without raising RuntimeError.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/testdb")
