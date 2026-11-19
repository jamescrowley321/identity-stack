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

DESCOPE_ONLY_ROUTE = (
    "the route serves Descope principals only (require_descope_principal, #566): it reads or writes "
    "Descope's roles, permissions, members, access keys, FGA or documents; canonical routes for "
    "other providers are #590"
)
DESCOPE_MANAGEMENT_API = "calls the Descope Management API itself; the run has no Descope project"
DESCOPE_ACCESS_KEY = "is about a Descope access-key session, which no other provider has"
TENANTS_CLAIM = (
    "the route reads Descope's tenants claim and serves Descope principals only (get_descope_claims): "
    "it answers this provider's principals 403; their tenants come from GET /api/identity"
)
ID_TOKEN_VALIDATION = (
    "POST /api/validate-id-token validates against the Descope provider only: 503 for this provider (#636)"
)
TENANT_SETTINGS = (
    "the Tenant Settings page loads GET /api/tenants/current/settings, the Descope tenant's custom "
    "attributes, which serves Descope principals only (#590)"
)
ORY_PROVIDER = "proves the Ory provider, which only the Descope run's backend configures"


def _both(reason: str) -> dict[str, str]:
    return {KEYCLOAK: reason, ZITADEL: reason}


def _each(module: str, tests: list[str], reasons: dict[str, str]) -> dict[str, dict[str, str]]:
    return {f"{module}::{test}": dict(reasons) for test in tests}


#: ``{test: {provider: reason}}``. A key is a module (``test_x.py``), a class
#: (``test_x.py::TestY``) or a test (``test_x.py::TestY::test_z``) under ``tests/e2e/``;
#: it covers every test below it. Descope has no entry: the suite was written for it.
NOT_APPLICABLE: dict[str, dict[str, str]] = {
    "test_ory_sso_e2e.py": {KEYCLOAK: ORY_PROVIDER, ZITADEL: ORY_PROVIDER},
    # Login, session and identity
    "test_api_authenticated.py::test_tenants_endpoint_lists_user_tenants": _both(TENANTS_CLAIM),
    "test_api_authenticated.py::test_validate_id_token": _both(ID_TOKEN_VALIDATION),
    "test_page_content.py::test_no_5xx_errors_on_dashboard": _both(ID_TOKEN_VALIDATION),
    "test_api_endpoints.py::TestAuthenticatedEndpoints::test_tenants_returns_200": _both(TENANTS_CLAIM),
    "test_async_db_migration.py::TestAsyncTenantEndpoints::test_list_tenants_returns_list": _both(TENANTS_CLAIM),
    "test_identity_crud_e2e.py::TestTenantOperations::test_list_tenants_returns_jwt_tenants": _both(TENANTS_CLAIM),
    "test_identity_crud_e2e.py::TestTenantOperations::test_get_current_tenant": _both(DESCOPE_ONLY_ROUTE),
    # Tenant resources
    **_each(
        "test_async_db_migration.py::TestAsyncTenantResourcesCRUD",
        ["test_tenant_resource_create_and_list", "test_tenant_resource_duplicate_returns_409"],
        _both(TENANTS_CLAIM),
    ),
    # RBAC administration
    **_each(
        "test_api_endpoints.py::TestAdminEndpoints",
        [
            "test_roles_list_responds",
            "test_permissions_list_responds",
            "test_members_list_responds",
            "test_keys_list_responds",
        ],
        _both(DESCOPE_ONLY_ROUTE),
    ),
    "test_identity_crud_e2e.py::TestPermissionCrudCanonical": _both(DESCOPE_ONLY_ROUTE),
    "test_identity_crud_e2e.py::TestRoleCrudCanonical": _both(DESCOPE_ONLY_ROUTE),
    "test_identity_crud_e2e.py::TestMemberCrud": _both(DESCOPE_ONLY_ROUTE),
    "test_identity_repository_api.py": _both(DESCOPE_ONLY_ROUTE),
    **_each(
        "test_rbac_api.py",
        [
            "test_runtime_permission_create_and_delete",
            "test_runtime_role_create_and_delete",
            "test_permission_crud_lifecycle",
            "test_role_crud_lifecycle",
        ],
        _both(DESCOPE_ONLY_ROUTE),
    ),
    **_each(
        "test_rbac_api.py",
        ["test_tf_seeded_roles_present", "test_tf_seeded_permissions_present", "test_runtime_role_assignment"],
        _both(DESCOPE_MANAGEMENT_API),
    ),
    "test_identity_repository_ui.py::test_navigate_to_settings_page": _both(TENANT_SETTINGS),
    "test_identity_repository_ui.py::test_role_created_via_api_is_invisible_to_an_access_key_session": _both(
        DESCOPE_ACCESS_KEY
    ),
    # Documents and FGA
    "test_async_db_migration.py::TestAsyncDocumentCRUD": _both(DESCOPE_ONLY_ROUTE),
    "test_async_db_migration.py::TestAsyncErrorResponses::test_invalid_document_id_format_returns_422": _both(
        DESCOPE_ONLY_ROUTE
    ),
    **_each(
        "test_async_db_migration.py::TestAsyncDocumentValidation",
        ["test_create_document_empty_title_rejected", "test_create_document_missing_title_rejected"],
        _both(DESCOPE_ONLY_ROUTE),
    ),
    **_each(
        "test_documents_e2e.py",
        [
            "test_create_document_creates_fga_owner",
            "test_owner_has_all_permissions",
            "test_editor_has_view_and_edit_not_delete",
            "test_viewer_has_view_only",
            "test_document_get_allowed_for_owner",
            "test_document_update_allowed_for_owner",
            "test_permission_derivation_through_endpoints",
            "test_document_fga_denies_without_relation",
            "test_document_delete_cleans_fga_relations",
            "test_list_documents_returns_authorized",
            "test_list_documents_excludes_unauthorized",
            "test_share_document_grants_access",
            "test_revoke_share_denies_access",
            "test_sequential_relation_revocation_no_stale_grants",
        ],
        _both(DESCOPE_ONLY_ROUTE),
    ),
    **_each(
        "test_fga_api.py",
        [
            "test_get_fga_schema",
            "test_relation_create_and_delete",
            "test_list_relations_empty",
            "test_check_permission_denied_for_nonexistent",
            "test_create_relation_validation",
            "test_list_relations_validation",
            "test_check_permission_validation",
            "test_relation_lifecycle_with_permission_check",
            "test_update_fga_schema",
        ],
        _both(DESCOPE_ONLY_ROUTE),
    ),
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
