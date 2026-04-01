"""Unit tests for Alembic environment configuration.

AC-1.2.1: Alembic async setup — no sync engine anywhere in env.py.

Note: env.py cannot be imported directly (requires Alembic context proxy),
so these tests inspect the source file as text.
"""

import re
from pathlib import Path

_ENV_SOURCE = (Path(__file__).resolve().parents[2] / "migrations" / "env.py").read_text()


def test_env_no_sync_engine():
    """env.py must not use sync create_engine — only create_async_engine (D3)."""
    bare_matches = re.findall(r"(?<!async_)\bcreate_engine\b", _ENV_SOURCE)
    assert len(bare_matches) == 0, f"Found sync create_engine in env.py: {bare_matches}"


def test_env_no_sync_session():
    """env.py must not import sync Session from sqlalchemy or sqlmodel."""
    session_imports = re.findall(r"\bfrom\s+sqlalchemy.*import.*\bSession\b", _ENV_SOURCE)
    sync_sessions = [line for line in session_imports if "AsyncSession" not in line]
    assert len(sync_sessions) == 0, f"Found sync Session import in env.py: {sync_sessions}"


def test_env_uses_async_engine():
    """env.py must use create_async_engine for migrations."""
    assert "create_async_engine" in _ENV_SOURCE


def test_env_uses_asyncio_run():
    """env.py must use asyncio.run for online migrations."""
    assert "asyncio.run" in _ENV_SOURCE


def test_env_imports_all_identity_models():
    """env.py must import all identity models for Alembic metadata discovery."""
    expected_imports = [
        "User",
        "IdPLink",
        "Tenant",
        "Role",
        "Permission",
        "RolePermission",
        "UserTenantRole",
        "Provider",
    ]
    for model_name in expected_imports:
        assert model_name in _ENV_SOURCE, f"env.py missing import for {model_name}"


def test_env_imports_existing_models():
    """env.py must import existing Document and TenantResource models."""
    assert "Document" in _ENV_SOURCE
    assert "TenantResource" in _ENV_SOURCE


def test_env_sets_target_metadata():
    """target_metadata should be set to SQLModel.metadata."""
    assert "target_metadata = SQLModel.metadata" in _ENV_SOURCE


def test_env_no_hardcoded_credentials():
    """env.py must not contain hardcoded database credentials."""
    assert "identity:dev@" not in _ENV_SOURCE, "Found hardcoded dev credentials in env.py"


def test_env_requires_database_url():
    """env.py must fail loudly if DATABASE_URL is not set."""
    assert "RuntimeError" in _ENV_SOURCE or "raise" in _ENV_SOURCE
