"""Shared E2E API test helpers — unique names and constants."""

import uuid


def unique_name(prefix: str) -> str:
    """Generate a unique name for test resources to avoid collisions."""
    return f"{prefix}-e2e-{uuid.uuid4().hex[:8]}"


def assert_refused_for_role(response) -> None:
    """403 because the caller lacks the role, never because it was taken for a machine (#699).

    The Descope run's member is a test user holding ``viewer`` alone, so its refusal is
    exactly the role check's. Another run's may be refused earlier, by the sync provider
    guard or the tenant resolver (``not_applicable.py``), but never as a machine.
    """
    import os

    from app.dependencies.rbac import INSUFFICIENT_PERMISSIONS, MACHINE_PRINCIPAL_REFUSED
    from tests.e2e.not_applicable import DESCOPE, selected_provider

    detail = response.json().get("detail") if response.status == 403 else response.text()
    if selected_provider(os.environ) == DESCOPE:
        assert (response.status, detail) == (403, INSUFFICIENT_PERMISSIONS)
    else:
        assert response.status == 403, f"{response.status}: {detail}"
        assert detail != MACHINE_PRINCIPAL_REFUSED
