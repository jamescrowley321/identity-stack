"""Fail-loud coverage gate for the authenticated E2E / UI Playwright suite.

Without this gate, missing Descope credentials make ~164 of ~220 E2E tests —
every authenticated flow, including *all* authenticated-UI Playwright tests
(dashboard, profile, identity-CRUD UI, RBAC, multi-IdP, documents) — SKIP
silently while the ``E2E Tests`` job still reports green. A UI PR then looks
covered when it never exercised the UI.

CI sets ``E2E_REQUIRE_AUTH=1`` so this gate turns the job RED whenever the
credentials that unlock the authenticated suite are absent or empty. Locally
(flag unset) it skips, so ``make test-e2e`` without credentials still works.
"""

import os

import pytest

# The credentials the authenticated fixtures gate on. Keep in sync with
# conftest.py (``_has_mgmt_key`` / ``_has_client_creds``) and the per-file
# ``skipif`` markers in test_authenticated_ui.py / test_identity_repository_ui.py
# / test_rbac_api.py.
REQUIRED_AUTH_ENV = (
    "DESCOPE_PROJECT_ID",      # discovery + mgmt auth header + token authority
    "DESCOPE_MANAGEMENT_KEY",  # _ensure_test_user + admin_access_token
    "DESCOPE_CLIENT_ID",       # auth_access_token (with DESCOPE_CLIENT_SECRET)
    "DESCOPE_CLIENT_SECRET",
    "E2E_TEST_EMAIL",          # ensure_test_user() raises RuntimeError if empty
    "E2E_TEST_TENANT_ID",      # tenant / RBAC-scoped UI flows
)


def _require_auth() -> bool:
    """Whether the authenticated E2E suite is mandatory for this run (CI)."""
    return os.environ.get("E2E_REQUIRE_AUTH", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )


@pytest.mark.skipif(
    not _require_auth(),
    reason="E2E_REQUIRE_AUTH not set — local/dev run, authenticated coverage optional",
)
def test_authenticated_suite_is_enabled():
    """The authenticated E2E/UI suite must actually run when required (CI).

    Fails loudly instead of letting the credentialed Playwright tests skip into
    a green, hollow ``E2E Tests`` job — so "E2E ✓" means the UI was exercised.
    """
    missing = sorted(k for k in REQUIRED_AUTH_ENV if not os.environ.get(k))
    assert not missing, (
        "Authenticated E2E/UI coverage is DISABLED — missing/empty credentials: "
        f"{missing}. The authenticated Playwright suite (dashboard, profile, "
        "identity-CRUD UI, RBAC, multi-IdP, documents) silently skips without "
        "these, so UI PRs are not exercised. Populate these secrets (or point "
        "the authenticated suite at a mock OP) to restore coverage."
    )
