# Identity providers compared

identity-stack runs against four identity providers: Descope and Ory (hosted), Keycloak and
Zitadel (self-hosted, in Docker). Cloudflare Access is planned
([identity-stack#531](https://github.com/jamescrowley321/identity-stack/issues/531)). This page
compares them area by area, one column per provider. Each provider's own page has the detail:
[keycloak.md](keycloak.md), [zitadel.md](zitadel.md), [architecture.md](architecture.md) for
Descope's token model, and [`infra/PROVIDERS.md`](../infra/PROVIDERS.md) for the Terraform roots
and credentials.

## Reading a cell

Every cell opens with one of five words:

| Status | Meaning | What the cell names |
|---|---|---|
| **Supported** | identity-stack does it with this provider | The test, recorded fixture or Terraform file of this repository that shows it |
| **Not supported** | The provider does not offer it | The recorded fixture or test that shows the provider does not |
| **Not built** | The provider offers it, or may, and identity-stack does not use it | The issue that tracks it |
| **Not applicable** | The question does not arise for this provider | Why |
| **Planned** | The provider is not integrated yet | The issue that tracks it |

No cell rests on a provider's documentation alone. Where nothing in this repository shows what a
hosted provider offers, the cell says **Not built** and names the issue. Paths are relative to the
repository root; `file::Name` names a test class or function in that file.
`backend/tests/unit/test_provider_matrix.py` fails when a cell has no status, a Supported or Not
supported cell names no file, a Not built or Planned cell names no issue, or a cited file or name
does not exist.

Versions recorded: Keycloak `quay.io/keycloak/keycloak:26.8.2`, Zitadel `v4.20.3`, Ory Hydra
`oryd/hydra:v2.2.0` (the local stand-in for Ory Network).

## Tokens

| Question | Descope | Ory | Keycloak | Zitadel | Cloudflare Access |
|---|---|---|---|---|---|
| Issuer shape | Supported: three forms, `https://api.descope.com/{project id}`, `https://api.descope.com/v1/apps/{project id}` and the bare project id, all on one project JWKS (`backend/tests/unit/test_provider_algorithms.py::TestDescopeIssuers`) | Supported: one issuer, the project URL, trailing slash dropped (`backend/tests/unit/test_middleware_ory.py::TestOryProvider`) | Supported: `<KC_HOSTNAME>/realms/<realm>`, no trailing slash (`keycloak/fixtures/discovery.json`, `backend/tests/unit/test_providers_keycloak.py::TestKeycloakConfig`) | Supported: the instance's base URL, no path, no trailing slash (`backend/tests/fixtures/zitadel/discovery.json`) | Planned (#533) |
| Access-token format | Supported: JWT with `dct` and `tenants` claims (`backend/tests/unit/test_rbac_dependencies.py::TestRequirePermission`) | Supported: JWT, the access-token strategy the Ory root sets (`infra/ory/modules/ory-oidc-app/variables.tf`, `backend/tests/integration/test_ory_end_to_end.py`) | Supported: JWT, one `aud`, `azp`, `organization` and `resource_access` (`keycloak/fixtures/access-token.json`, `backend/tests/unit/test_keycloak_token_model.py::TestParsing`) | Supported: JWT, which the application must ask for; Zitadel's default is opaque, and an opaque token is refused (`backend/tests/fixtures/zitadel/defaults.json`, `backend/tests/unit/test_zitadel_token_binding.py::TestTheRecordedClaimSets`) | Planned (#533) |
| Signing algorithms | Supported: `RS256` only (`backend/tests/unit/test_provider_algorithms.py::TestDescope`) | Supported: `RS256` only (`backend/tests/unit/test_provider_algorithms.py::TestOry`, `backend/tests/integration/test_ory_algorithms.py`) | Supported: `RS256` only, though the realm publishes more (`backend/tests/unit/test_provider_algorithms.py::TestKeycloak`, `keycloak/fixtures/discovery.json`) | Supported: `RS256` only, the one algorithm the instance's keys use (`backend/tests/unit/test_zitadel_startup.py`, `backend/tests/fixtures/zitadel/jwks.json`) | Planned (#533) |
| Audience and client binding | Supported: `aud` is the project id on the URL issuers; a bare-issuer token has no `aud` and is held to its `drn` instead (`backend/tests/unit/test_provider_algorithms.py::TestDescopeIssuers`). No client binding beyond that | Supported: `aud` must name `ORY_AUDIENCE`, refused at startup when unset (`backend/tests/integration/test_ory_end_to_end.py::test_ory_token_for_a_different_audience_is_rejected`, `backend/tests/unit/test_providers_keycloak.py::TestConfiguredProviders`). No client binding | Supported: `aud` is exactly one configured type client and `azp` the app client configured for it (`backend/tests/unit/test_keycloak_token_model.py::TestSingleAudience`, `backend/tests/unit/test_keycloak_token_model.py::TestTokenBinding`) | Supported: `aud` contains the project id and `client_id` is the SPA's (`backend/tests/unit/test_zitadel_token_binding.py::TestEveryOtherTokenIsRefused`) | Planned (#533) |
| Access token told from ID token | Not built (#643) | Not built (#643) | Supported: the app's ID token names the app client in `aud`, so the binding refuses it (`backend/tests/unit/test_keycloak_token_model.py::TestTokenBinding`) | Supported: a token carrying `azp`, `at_hash`, `auth_time`, `sid` or `nonce` is refused (`backend/tests/unit/test_zitadel_token_binding.py::TestEveryOtherTokenIsRefused`, `backend/tests/e2e_zitadel/test_zitadel_e2e.py::test_a_live_id_token_is_refused`) | Planned (#533) |
| Access-token lifetime | Supported: 10 minutes by default, set by the Terraform root (`infra/variables.tf`, `infra/project.tf`) | Not applicable: set in the hosted Ory project; no root here sets it | Supported: 300 s, the realm's default (`keycloak/fixtures/access-token.json`) | Supported: 15 minutes, set by the Terraform root (`infra/zitadel/variables.tf`) | Planned (#533) |
| Refresh | Supported: the SPA renews silently (`frontend/src/config/__tests__/oidc.test.ts`) | Supported: the SPA renews silently (`frontend/src/config/__tests__/oidc.test.ts`) | Supported: a refresh keeps the token's organization and cannot switch it (`keycloak/fixtures/README.md`, `frontend/src/config/__tests__/oidc.test.ts`) | Supported: `offline_access` gives a refresh token the SPA renews with (`frontend/src/config/__tests__/oidc.zitadel.test.ts`, `backend/tests/e2e_zitadel/test_zitadel_e2e.py::test_logout_drops_the_tokens_and_the_next_sign_in_asks_for_credentials`) | Planned (#534) |
| Revocation before expiry | Not built (#542) | Not built (#542) | Not built (#542): an organization-bound token is checked against the directory on every request, but a portable token stays valid until it expires | Not built (#542): the access token and the refresh token outlive logout | Planned (#533) |
| `POST /api/validate-id-token` | Supported (`backend/tests/unit/test_validate_id_token.py`) | Not built (#636) | Not built (#636) | Not built (#636) | Planned (#533) |

## Tenancy

| Question | Descope | Ory | Keycloak | Zitadel | Cloudflare Access |
|---|---|---|---|---|---|
| What a tenant is | Supported: a Descope tenant (`infra/tenants.tf`) | Supported: a canonical tenant; Ory's token names none (`backend/tests/unit/test_rbac_dependencies.py::TestCanonicalRoleResolution`) | Supported: an Organization, its alias the tenant (`keycloak/realm/identity-stack-realm.json`, `backend/tests/unit/test_keycloak_tenant_access.py::TestTheOrganizationIsTheTenant`) | Supported: a canonical tenant linked to one Organization by `external_org_id` (`infra/zitadel/main.tf`, `backend/tests/integration/test_seed_zitadel.py`) | Planned (#533) |
| Where the request's tenant comes from | Supported: the token's `dct` claim; none is 403 (`backend/tests/unit/test_tenant_dependencies.py::TestGetTenantId`) | Supported: the sole canonical membership, or the one `X-Tenant-ID` selects among several (`backend/tests/unit/test_tenant_resolution.py`) | Supported: the token's `organization` claim, exactly one alias (`backend/tests/unit/test_keycloak_token_model.py::TestOrganization`) | Supported: `X-Tenant-ID` among the canonical memberships, as for Ory (`backend/tests/e2e_zitadel/test_zitadel_e2e.py::test_the_linked_user_is_admin_in_one_tenant_and_viewer_after_switching_to_the_other`) | Planned (#533) |
| Switching tenant | Not built (#647): the SPA cannot ask Descope for another `dct` | Supported: the SPA's switch names the tenant in `X-Tenant-ID` (`frontend/src/components/layout/__tests__/TenantSwitcher.test.tsx`, `backend/tests/unit/test_tenant_resolution.py`) | Supported: a new authorize request for `organization:<alias>` on the live session, no login form (`backend/tests/e2e_keycloak/test_keycloak_e2e.py::test_the_two_organization_member_logs_in_switches_organization_and_logs_out`) | Supported: the SPA's switch names the tenant in `X-Tenant-ID` (`backend/tests/e2e_zitadel/test_zitadel_e2e.py::test_the_linked_user_is_admin_in_one_tenant_and_viewer_after_switching_to_the_other`) | Planned (#534) |
| First-login provisioning | Supported: the Descope flow sync and webhooks create the canonical user (`backend/tests/unit/test_inbound_sync_service.py`) | Supported: `GET /api/identity` provisions a verified email just in time (`backend/tests/integration/test_ory_end_to_end.py::test_ory_jwt_authenticates_and_jit_provisions_canonical_identity`) | Supported: opt-in (`JIT_ENABLED_PROVIDERS`), no canonical role given, refused without an organization (`backend/tests/integration/test_keycloak_identity.py`) | Supported: the token's organization decides the one `member` grant, and only for an email in the tenant's domains (`backend/tests/integration/test_zitadel_provisioning.py::TestTheOrganizationsTenant`, `backend/tests/e2e_zitadel/test_zitadel_e2e.py::test_a_first_login_creates_a_member_of_the_home_tenant_only`) | Planned (#533) |
| Self-registration into a tenant | Not applicable: the Descope project's own setting | Not applicable: Ory tenants are canonical only | Supported: off in the realm (`keycloak/realm/identity-stack-realm.json`) | Supported: off in every tenant organization (`backend/tests/e2e_zitadel/test_zitadel_e2e.py::test_registration_in_a_tenant_organization_is_refused`) | Planned (#533) |

## Authorization

| Question | Descope | Ory | Keycloak | Zitadel | Cloudflare Access |
|---|---|---|---|---|---|
| Source of roles and permissions | Supported: the token's `tenants` claim (`backend/tests/unit/test_rbac_dependencies.py::TestDescopePathIsUnchanged`) | Supported: the canonical store (`backend/tests/unit/test_rbac_dependencies.py::TestCanonicalRoleResolution`) | Supported: the token, client roles of the type client as permissions (`backend/tests/unit/test_keycloak_tenant_access.py::TestClientRolesAreThePermissions`) | Supported: the canonical store; a roles claim on the token is never read (`backend/tests/unit/test_zitadel_canonical_roles.py::TestTheCanonicalStoreDecides`) | Planned (#533) |
| `require_role` | Supported (`backend/tests/unit/test_rbac_dependencies.py::TestRequireRole`) | Supported (`backend/tests/e2e/test_ory_sso_e2e.py::test_ory_principal_with_the_canonical_role_reaches_the_gated_route`) | Not applicable: the token model has no roles, so a role gate answers 403 (`backend/tests/unit/test_keycloak_tenant_access.py::TestClientRolesAreThePermissions::test_a_role_gated_route_refuses_a_keycloak_principal`); gating by permission is #541 | Supported (`backend/tests/e2e_zitadel/test_zitadel_e2e.py::test_the_linked_user_is_admin_in_one_tenant_and_viewer_after_switching_to_the_other`) | Planned (#533) |
| `require_permission` | Supported (`backend/tests/unit/test_rbac_dependencies.py::TestRequirePermission`) | Supported (`backend/tests/unit/test_rbac_dependencies.py::TestCanonicalRoleResolution`) | Supported, portable types for their read permissions only and organization-bound types for a user in one organization (`backend/tests/e2e_keycloak/test_keycloak_e2e.py::test_the_tenant_admin_of_one_organization_is_allowed_there`, `backend/tests/e2e_keycloak/test_keycloak_e2e.py::test_the_tenant_admin_of_two_organizations_is_refused_in_each`) | Supported (`backend/tests/unit/test_zitadel_canonical_roles.py::TestTheCanonicalStoreDecides`) | Planned (#533) |
| Fine-grained authorization | Supported: Descope FGA (`backend/tests/unit/test_fga_router.py`, `backend/tests/unit/test_fga_dependencies.py`) | Not built (#517) | Not built (#517) | Not built (#517) | Planned (#533) |
| Descope-backed routes (roles, permissions, members, access keys, documents) | Supported (`backend/tests/unit/test_descope_backed_routes.py`) | Not built (#590): refused, 403 | Not built (#590): refused, 403 | Not built (#590): refused, 403 | Planned (#533) |

## Admin operations

What the backend pushes to the provider when the canonical model changes, through the adapter
`SYNC_PROVIDER` selects.

| Operation | Descope | Ory | Keycloak | Zitadel | Cloudflare Access |
|---|---|---|---|---|---|
| Adapter | Supported: `DescopeSyncAdapter`, the default (`backend/tests/unit/test_zitadel_sync_selection.py::test_the_default_provider_is_descope`) | Not built (#644): `OrySyncAdapter` exists but `SYNC_PROVIDER` cannot select it | Supported: `KeycloakSyncAdapter` (`backend/tests/unit/test_sync_adapter_selection.py`) | Supported: `ZitadelSyncAdapter` (`backend/tests/unit/test_zitadel_sync_selection.py::test_a_zitadel_deployment_gets_the_zitadel_adapter`) | Planned (#533) |
| Create a user | Supported: invited through the Descope-backed member route, not the adapter (`backend/tests/unit/test_users_router.py::test_invite_member`) | Not built (#644) | Supported (`backend/tests/unit/test_keycloak_sync_adapter.py::TestSyncUser`, `backend/tests/integration/test_keycloak_sync_adapter.py::TestUsers`) | Supported (`backend/tests/unit/test_zitadel_sync_adapter.py::TestSyncUserCreate`, `backend/tests/integration_zitadel/test_zitadel_sync_adapter.py`) | Planned (#533) |
| Change a user's email | Not built (#649): the adapter pushes a user's status only | Not built (#644) | Supported: set unverified (`backend/tests/unit/test_keycloak_sync_adapter.py::TestSyncUser`) | Not built (#646) | Planned (#533) |
| Deactivate or activate a user | Supported (`backend/tests/unit/test_descope_adapter.py::TestSyncUser::test_sync_inactive_user_calls_disabled`) | Not built (#644) | Supported (`backend/tests/unit/test_keycloak_sync_adapter.py::TestSyncUser`) | Supported (`backend/tests/unit/test_zitadel_sync_adapter.py::TestSyncUserStatus`) | Planned (#533) |
| Delete a user | Not built (#649): the adapter's delete sends nothing | Not built (#644) | Supported (`backend/tests/unit/test_keycloak_sync_adapter.py::TestDeleteUser`) | Supported (`backend/tests/unit/test_zitadel_sync_adapter.py::TestDeleteUser`) | Planned (#533) |
| Create a tenant | Supported (`backend/tests/unit/test_descope_adapter.py::TestSyncTenant`) | Not built (#644) | Supported: an Organization whose alias is the tenant id (`backend/tests/unit/test_keycloak_sync_adapter.py::TestSyncTenant`, `backend/tests/integration/test_keycloak_sync_adapter.py::TestOrganizations`) | Supported: an Organization closed to self-registration, granted the project, then linked (`backend/tests/unit/test_zitadel_sync_adapter.py::TestSyncTenant`) | Planned (#533) |
| Update a tenant | Not built (#649): the adapter creates the tenant again | Not built (#644) | Supported: name and domains (`backend/tests/unit/test_keycloak_sync_adapter.py::TestSyncTenant`) | Not built (#645): a linked tenant is left alone | Planned (#533) |
| Delete a tenant | Not built (#649): the adapter's delete sends nothing | Not built (#644) | Supported (`backend/tests/unit/test_keycloak_sync_adapter.py::TestDeleteTenant`) | Not built (#645) | Planned (#533) |
| Tenant membership | Supported: role assignments pushed (`backend/tests/unit/test_descope_adapter.py::TestDeleteOperations::test_sync_role_assignment`) | Not built (#644) | Supported: the first role in a tenant adds the Organization membership, the last one removed ends it (`backend/tests/unit/test_keycloak_sync_adapter.py::TestMembers`, `backend/tests/integration/test_keycloak_sync_adapter.py::TestMembers`) | Not applicable: memberships are canonical by design; a user stays in the organization it was created in and is granted nothing there (`backend/tests/unit/test_zitadel_sync_adapter.py::TestCanonicalOnly`) | Planned (#533) |
| Role and permission definitions | Supported (`backend/tests/unit/test_descope_adapter.py::TestSyncRole`, `backend/tests/unit/test_descope_adapter.py::TestSyncPermission`) | Not applicable: canonical (`backend/tests/unit/test_ory_adapter.py::TestCanonicalOnlyNoOps`) | Not applicable: canonical; client roles are the realm admin's (`backend/tests/unit/test_keycloak_sync_adapter.py::TestCanonicalOnly`) | Not applicable: canonical (`backend/tests/unit/test_zitadel_sync_adapter.py::TestCanonicalOnly`) | Planned (#533) |
| Access keys | Supported: Descope access keys (`backend/tests/unit/test_accesskeys_router.py`) | Not built (#543) | Not built (#543) | Not built (#543) | Planned (#533) |
| Inbound changes and reconciliation | Supported: webhooks and a reconciliation run (`backend/tests/unit/test_inbound_sync_service.py`, `backend/tests/unit/test_reconciliation_service.py`) | Not built (#518) | Not built (#518): reconciliation is refused, 409 | Not built (#518) | Planned (#533) |

## Flows and protocol features

| Feature | Descope | Ory | Keycloak | Zitadel | Cloudflare Access |
|---|---|---|---|---|---|
| Authorization code with PKCE | Supported: the SPA's OIDC client, code flow (`frontend/src/config/__tests__/oidc.test.ts`) | Supported: a real code exchange against `hydra-test` (`backend/tests/integration/test_ory_end_to_end.py`) | Supported: S256 through the login form (`keycloak/fixtures/access-token.json`, `backend/tests/e2e_keycloak/test_keycloak_e2e.py`) | Supported: S256, the only method the instance offers (`backend/tests/fixtures/zitadel/discovery.json`, `backend/tests/e2e_zitadel/test_zitadel_e2e.py`) | Planned (#534) |
| Client credentials | Supported: an access key exchanged for a bare-issuer session token (`backend/tests/unit/test_provider_algorithms.py::TestDescopeIssuers`) | Not built (#543) | Not built (#543) | Not built (#543): a machine user's token passes every check but the client binding and is refused (`backend/tests/integration_zitadel/test_zitadel_machine_token.py`) | Planned (#533) |
| Logout | Supported: the user's sessions revoked through the Management API (`backend/tests/unit/test_auth_router.py::test_logout_succeeds_with_valid_token`) | Supported: RP-initiated (`backend/tests/unit/test_logout_providers.py::TestOryResponseIsUnchanged`) | Supported: RP-initiated, with the token's app client (`backend/tests/unit/test_logout_providers.py::TestKeycloakLogout`, `backend/tests/e2e_keycloak/test_keycloak_e2e.py::test_the_two_organization_member_logs_in_switches_organization_and_logs_out`) | Supported: RP-initiated with `id_token_hint` (`backend/tests/unit/test_logout_providers.py::TestZitadelLogout`, `backend/tests/e2e_zitadel/test_zitadel_e2e.py::test_logout_drops_the_tokens_and_the_next_sign_in_asks_for_credentials`) | Planned (#534) |
| Back-channel logout | Not built (#642) | Not built (#642) | Not built (#642): the realm advertises it (`keycloak/fixtures/discovery.json`) | Not built (#642): the instance advertises it (`backend/tests/fixtures/zitadel/discovery.json`) | Planned (#533) |
| DPoP | Not built (#639) | Not built (#639) | Not built (#639): the realm advertises it (`keycloak/fixtures/discovery.json`) | Not supported: not advertised (`backend/tests/fixtures/zitadel/discovery.json`) | Planned (#533) |
| Mutual TLS | Not built (#639) | Not built (#639) | Not built (#639): the realm advertises certificate-bound tokens (`keycloak/fixtures/discovery.json`) | Not supported: not advertised (`backend/tests/fixtures/zitadel/discovery.json`) | Planned (#533) |
| PAR | Not built (#640) | Not built (#640) | Not built (#640): the realm advertises it (`keycloak/fixtures/discovery.json`) | Not supported: not advertised (`backend/tests/fixtures/zitadel/discovery.json`) | Planned (#533) |
| RAR | Not built (#641) | Not built (#641) | Not supported: no `authorization_details_types_supported` (`keycloak/fixtures/discovery.json`) | Not supported: no `authorization_details_types_supported` (`backend/tests/fixtures/zitadel/discovery.json`) | Planned (#533) |
| Token exchange | Not built (#544) | Not built (#544) | Not built (#544): the realm advertises the grant (`keycloak/fixtures/discovery.json`) | Not supported: not among the grant types (`backend/tests/fixtures/zitadel/discovery.json`) | Planned (#533) |

## Operations

| Question | Descope | Ory | Keycloak | Zitadel | Cloudflare Access |
|---|---|---|---|---|---|
| Hosting | Supported: a hosted Descope project, built by the Terraform root (`infra/main.tf`, `infra/project.tf`); no container here | Supported: a hosted Ory Network project, built by the Terraform root (`infra/ory/main.tf`); `hydra-test`, Ory's OAuth2 server, stands in for it in tests (`docker-compose.test.yml`) | Supported: self-hosted (`docker-compose.yml`) | Supported: self-hosted (`docker-compose.yml`) | Planned (#531) |
| Compose profile | Not applicable: hosted | Not applicable: hosted; `hydra-test` in the test stack stands in for it (`docker-compose.test.yml`) | Supported: `keycloak`, and `keycloak-test` in the test stack (`docker-compose.yml`, `docker-compose.test.yml`) | Supported: `zitadel` (`docker-compose.yml`) | Planned (#531) |
| Terraform root | Supported: `infra/`, HCP Terraform workspace (`infra/main.tf`) | Supported: `infra/ory/`, local state (`infra/ory/main.tf`) | Supported: `infra/keycloak/`, local state, checked against the import in CI (`infra/keycloak/main.tf`) | Supported: `infra/zitadel/`, local state (`infra/zitadel/main.tf`) | Planned (#532) |
| Containers | Not applicable: hosted | Supported: one, `oryd/hydra:v2.2.0`, tests only (`docker-compose.test.yml`) | Supported: one, `quay.io/keycloak/keycloak:26.8.2` (`docker-compose.yml`) | Supported: four, `zitadel-proxy`, `zitadel-api`, `zitadel-login`, `zitadel-db` (`docker-compose.yml`) | Planned (#531) |
| Licence | Not applicable: a commercial hosted service | Supported: Hydra is Apache-2.0, run unmodified (`infra/ory/README.md`) | Supported: Apache-2.0, run unmodified (`docs/keycloak.md`) | Supported: AGPL-3.0, run unmodified (`docs/zitadel.md`) | Planned (#531) |

## Test coverage

| Suite | Descope | Ory | Keycloak | Zitadel | Cloudflare Access |
|---|---|---|---|---|---|
| Unit | Supported (`backend/tests/unit/test_descope_adapter.py`, `backend/tests/unit/test_provider_algorithms.py`) | Supported (`backend/tests/unit/test_middleware_ory.py`, `backend/tests/unit/test_ory_adapter.py`) | Supported (`backend/tests/unit/test_keycloak_token_model.py`, `backend/tests/unit/test_keycloak_tenant_access.py`, `backend/tests/unit/test_keycloak_sync_adapter.py`) | Supported (`backend/tests/unit/test_zitadel_token_binding.py`, `backend/tests/unit/test_zitadel_first_login.py`, `backend/tests/unit/test_zitadel_sync_adapter.py`) | Planned (#533) |
| Integration | Supported, with Descope credentials (`backend/tests/integration/test_api.py`) | Supported, against `hydra-test` (`backend/tests/integration/test_ory_end_to_end.py`) | Supported, against the import and the Terraform realm (`backend/tests/integration/test_keycloak_token_config.py`, `backend/tests/integration/test_keycloak_sync_adapter.py`) | Supported, against the compose instance (`backend/tests/integration/test_zitadel_provisioning.py`, `backend/tests/integration_zitadel/test_zitadel_sync_adapter.py`) | Planned (#535) |
| End-to-end UI | Supported (`backend/tests/e2e/test_authenticated_ui.py`) | Not built (#648) | Supported: the shared suite and its own (`backend/tests/e2e/not_applicable.py`, `backend/tests/e2e_keycloak/test_keycloak_e2e.py`) | Supported: the shared suite and its own (`backend/tests/e2e/not_applicable.py`, `backend/tests/e2e_zitadel/test_zitadel_e2e.py`) | Planned (#535) |
| End-to-end API | Supported (`backend/tests/e2e/test_api_authenticated.py`) | Supported: real tokens against the shipped backend (`backend/tests/e2e/test_ory_sso_e2e.py`) | Supported: the shared suite with `E2E_PROVIDER=keycloak` (`backend/tests/e2e/not_applicable.py`) | Supported: the shared suite with `E2E_PROVIDER=zitadel` (`backend/tests/e2e/not_applicable.py`) | Planned (#535) |
| CI without credentials | Not applicable: a hosted project; its jobs read the `DESCOPE_*` secrets (`.github/workflows/ci.yml`) | Supported: `hydra-test` needs none, though its e2e module runs in the Descope `E2E Tests` job (`.github/workflows/ci.yml`) | Supported: `E2E Tests (Keycloak)`, `Keycloak Terraform Check` and the Keycloak integration tests (`.github/workflows/ci.yml`) | Supported: `E2E Tests (Zitadel)` and `Zitadel Terraform Check` (`.github/workflows/ci.yml`) | Planned (#535) |

## Gaps between Keycloak and Zitadel

Where one of the two self-hosted providers does something the other does not, the reason is one of:

- **Built for one only**, with an issue for the other: Zitadel tenant update and delete (#645) and
  email change (#646), which the Keycloak adapter pushes.
- **By design**: Keycloak authorizes from the token (client roles, Organizations) and Zitadel from
  the canonical store, so Keycloak has no `require_role` and Zitadel pushes no tenant memberships
  ([ADR-0010](adr/0010-keycloak-token-model.md), [zitadel.md](zitadel.md#admin-adapter)).
- **The provider does not offer it**: DPoP, mutual TLS, PAR and token exchange are advertised by
  Keycloak and not by Zitadel. identity-stack uses none of them yet (#639, #640, #544).

Everything else is the same for both, including what neither has yet: revocation before expiry
(#542), back-channel logout (#642), client credentials (#543), fine-grained authorization (#517)
and `POST /api/validate-id-token` (#636).
