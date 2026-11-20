"""Which provider the shared end-to-end suite runs against, and what does not apply to it.

The suite in ``tests/e2e/`` runs against one identity provider per run, chosen by
``E2E_PROVIDER`` (``descope`` when unset). A test that cannot apply to a provider
is listed in ``NOT_APPLICABLE`` with the reason, and the run deselects it and
prints the list (``conftest.py``). No test is skipped by environment instead.

This module imports nothing from playwright, so the unit job can check the table
(``tests/unit/test_e2e_not_applicable.py``).
"""

import os
from collections.abc import Mapping

PROVIDER_VARIABLE = "E2E_PROVIDER"
DESCOPE = "descope"
KEYCLOAK = "keycloak"
ZITADEL = "zitadel"
PROVIDERS = (DESCOPE, KEYCLOAK, ZITADEL)

# The suite's directory, relative to the backend: table keys are relative to it.
SUITE = "tests/e2e/"


class UnknownE2EProvider(ValueError):
    """``E2E_PROVIDER`` names no provider the suite knows."""


def selected_provider(environment: Mapping[str, str]) -> str:
    """The provider named by ``E2E_PROVIDER``: ``descope`` when unset or empty."""
    name = environment.get(PROVIDER_VARIABLE, "").strip() or DESCOPE
    if name not in PROVIDERS:
        raise UnknownE2EProvider(f"{PROVIDER_VARIABLE}={name!r} is not one of {', '.join(PROVIDERS)}")
    return name


def descope_credentials_missing(*names: str, environment: Mapping[str, str] = os.environ) -> bool:
    """True in a Descope run without every one of ``names``; False in any other provider's run.

    The Descope-era modules skip locally when the Descope credentials they use are
    absent (CI fails instead: ``DESCOPE_CI_REQUIRED_ENV``). Another provider's run
    needs none of them, so the condition must not skip its tests.
    """
    return selected_provider(environment) == DESCOPE and not all(environment.get(name) for name in names)


#: The backend's internal-endpoint secrets. The Descope-era internal-endpoint
#: tests compute their headers and HMACs from them and skip a test when one is
#: unset, so a Keycloak or Zitadel run requires all three up front instead.
INTERNAL_ENDPOINT_SECRETS = ("DESCOPE_FLOW_SYNC_SECRET", "DESCOPE_WEBHOOK_SECRET", "INTERNAL_IDENTITY_KEY")


def missing_internal_endpoint_secrets(provider: str, environment: Mapping[str, str] = os.environ) -> list[str]:
    """The ``INTERNAL_ENDPOINT_SECRETS`` unset in ``provider``'s run; none for Descope, whose tests skip instead."""
    if provider == DESCOPE:
        return []
    return [name for name in INTERNAL_ENDPOINT_SECRETS if not environment.get(name)]


# --- Reasons -------------------------------------------------------------------------
#
# One sentence each, saying what about the provider makes the test inapplicable.

DEFINITIONS_NEED_OPERATOR = (
    "changing the role and permission definitions needs operator from a canonical principal's sole membership "
    "(require_definitions_role, #676), and the run's admin is the two-tenant fixture user, a tenant admin: "
    "no fixture user of this provider is an operator (#710)"
)
NOT_THE_SYNC_PROVIDER = (
    "the member, role and permission routes serve the sync provider's principals "
    "(require_sync_provider_principal, #676, #677) and this run's backend syncs to Descope (SYNC_PROVIDER unset: "
    "it holds no Zitadel write credential), so they answer its principals 403 (#709)"
)
NO_SYNC_PROVIDER_PAGES = (
    "this run's backend syncs to Descope, so its principals are no sync provider principals "
    "(GET /api/capabilities) and the SPA has no Members or Roles page for them (#683, #709)"
)
DEFINITIONS_NOT_SYNCED = f"{NOT_THE_SYNC_PROVIDER}; changing the definitions also needs operator (#710)"
DESCOPE_MANAGEMENT_API = "calls the Descope Management API itself; the run has no Descope project"
DESCOPE_ACCESS_KEY = "is about a Descope access-key session, which no other provider has"
NO_CANONICAL_IDENTITY = (
    "is about the Descope admin, a test user with no canonical identity; this provider's admin is a "
    "seeded canonical user"
)
ORY_PROVIDER = "proves the Ory provider, which only the Descope run's backend configures"
NO_CUSTOM_ATTRIBUTES = (
    "this run's backend syncs with Descope, whose custom attributes serve Descope principals only, so "
    "its principals have no custom_attributes capability and the SPA no Tenant Settings page (#678, #679)"
)
NO_MACHINE_CREDENTIALS = (
    "this run's backend syncs with Descope, whose access keys serve Descope principals only, so its "
    "principals have no machine_credentials capability and the SPA no Access Keys page (#678, #680)"
)


def _both(reason: str) -> dict[str, str]:
    return {KEYCLOAK: reason, ZITADEL: reason}


DEFINITIONS = {KEYCLOAK: DEFINITIONS_NEED_OPERATOR, ZITADEL: DEFINITIONS_NOT_SYNCED}


def _each(module: str, tests: list[str], reasons: dict[str, str]) -> dict[str, dict[str, str]]:
    return {f"{module}::{test}": dict(reasons) for test in tests}


#: ``{test: {provider: reason}}``. A key is a module (``test_x.py``), a class
#: (``test_x.py::TestY``) or a test (``test_x.py::TestY::test_z``) under ``tests/e2e/``;
#: it covers every test below it. Descope has no entry: the suite was written for it.
NOT_APPLICABLE: dict[str, dict[str, str]] = {
    "test_ory_sso_e2e.py": {KEYCLOAK: ORY_PROVIDER, ZITADEL: ORY_PROVIDER},
    # Login, session and identity
    # RBAC administration. A tenant admin lists the definitions (#707); changing them needs operator.
    **_each(
        "test_api_endpoints.py::TestAdminEndpoints",
        ["test_roles_list_responds", "test_permissions_list_responds"],
        {ZITADEL: NOT_THE_SYNC_PROVIDER},
    ),
    "test_identity_crud_e2e.py::TestPermissionCrudCanonical": DEFINITIONS,
    "test_identity_crud_e2e.py::TestRoleCrudCanonical": DEFINITIONS,
    # The member routes serve the sync provider's principals (#677): Keycloak's run
    # syncs to Keycloak and runs them; Zitadel's backend syncs to Descope.
    "test_api_endpoints.py::TestAdminEndpoints::test_members_list_responds": {ZITADEL: NOT_THE_SYNC_PROVIDER},
    "test_identity_crud_e2e.py::TestMemberCrud": {ZITADEL: NOT_THE_SYNC_PROVIDER},
    "test_identity_repository_api.py": DEFINITIONS,
    **_each(
        "test_rbac_api.py",
        [
            "test_runtime_permission_create_and_delete",
            "test_runtime_role_create_and_delete",
            "test_permission_crud_lifecycle",
            "test_role_crud_lifecycle",
        ],
        DEFINITIONS,
    ),
    **_each(
        "test_rbac_api.py",
        ["test_tf_seeded_roles_present", "test_tf_seeded_permissions_present", "test_runtime_role_assignment"],
        _both(DESCOPE_MANAGEMENT_API),
    ),
    # Pages a capability gives (test_capability_pages.py checks each run sees exactly its own)
    "test_identity_repository_ui.py::test_navigate_to_roles_page": {ZITADEL: NO_SYNC_PROVIDER_PAGES},
    "test_identity_repository_ui.py::test_navigate_to_members_page": {ZITADEL: NO_SYNC_PROVIDER_PAGES},
    "test_page_content.py::test_members_page_loads": {ZITADEL: NO_SYNC_PROVIDER_PAGES},
    "test_page_content.py::test_roles_page_loads": {ZITADEL: NO_SYNC_PROVIDER_PAGES},
    "test_identity_repository_ui.py::test_navigate_to_settings_page": {ZITADEL: NO_CUSTOM_ATTRIBUTES},
    "test_authenticated_ui.py::test_navigate_to_settings": {ZITADEL: NO_CUSTOM_ATTRIBUTES},
    "test_page_content.py::test_tenant_settings_handles_no_tenant": {ZITADEL: NO_CUSTOM_ATTRIBUTES},
    # Keycloak's machine credentials are canonical clients of the realm (#680): both run there.
    "test_page_content.py::test_access_keys_page_loads": {ZITADEL: NO_MACHINE_CREDENTIALS},
    "test_api_endpoints.py::TestAdminEndpoints::test_keys_list_responds": {ZITADEL: NO_MACHINE_CREDENTIALS},
    "test_identity_repository_ui.py::test_role_created_via_api_is_invisible_to_an_admin_with_no_identity": _both(
        NO_CANONICAL_IDENTITY
    ),
    "test_descope_access_key.py": _both(DESCOPE_ACCESS_KEY),
    # Documents and FGA run for every provider: OpenFGA serves the canonical runs' principals (#682).
}


def reason_not_applicable(nodeid: str, provider: str) -> str | None:
    """Why the test ``nodeid`` (as pytest names it from the backend) does not apply, or None."""
    if not nodeid.startswith(SUITE):
        return None
    test = nodeid.removeprefix(SUITE)
    for key, reasons in NOT_APPLICABLE.items():
        if provider in reasons and (test == key or test.startswith((f"{key}::", f"{key}["))):
            return reasons[provider]
    return None
