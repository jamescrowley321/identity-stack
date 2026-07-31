"""Shared E2E API test helpers — unique names and constants."""

import uuid


def unique_name(prefix: str) -> str:
    """Generate a unique name for test resources to avoid collisions."""
    return f"{prefix}-e2e-{uuid.uuid4().hex[:8]}"
