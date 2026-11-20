# Backend conventions

The house rules for `backend/app/`. Cite a rule by its section number, for example
"conventions §12". The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and
**MAY** are to be interpreted as described in BCP 14 ([RFC 2119], [RFC 8174]) when, and
only when, they appear in all capitals, as shown here.

The rules describe the code as it is. Where a module has to break a rule, the rule names
it. An exception that is not named is a defect, not a precedent. The decisions behind
the larger rules are recorded in [`docs/adr/`](adr/README.md). The helpers that enforce
the rules carry further, narrower rules in their docstrings. Read a helper's docstring
before you change the helper or call it in a new way.

Enforcement is noted under each rule. **Ruff** and **import-linter** mean `make lint`
fails, and **test** means `make test-unit` fails. Rules marked **review** are enforced by a
reviewer.

[RFC 2119]: https://www.rfc-editor.org/rfc/rfc2119
[RFC 8174]: https://www.rfc-editor.org/rfc/rfc8174

## Configuration

### 1. Configuration is read only through `app.knobs`

Code in `app/` **MUST** read environment variables with the helpers in `app/knobs.py`:
`knob`, `required_knob`, `knob_flag` and `knob_set`. It **MUST NOT** use `os.environ`
or `os.getenv`.

- Exception: `app/knobs.py` itself.
- Outside `app/`, the rule does not apply to `tests/`, `scripts/` or
  `migrations/env.py`, which run outside the application process.

A value fixed for the process lifetime is assigned to a module-level constant, for example
`RATE_LIMIT_AUTH = knob("RATE_LIMIT_AUTH", "10/minute")`. A value that must track the
environment is read inside the function that uses it. The helpers read the environment
when they are called, so the call site decides which of the two it gets. See
[ADR-0005](adr/0005-central-settings-module.md).

*Enforcement:* Ruff (TID251), plus a test (`test_house_rules.py::test_only_knobs_reads_the_environment`).

### 2. Every variable is documented with its default

Every variable name passed to a knob helper **MUST** have a row in
[`environment-variables.md`](environment-variables.md). The row's default **MUST** be the
literal default given at the call site. A variable's name or default **MUST NOT** change
unless the row changes in the same commit, because names and defaults are part of the
deployment contract.

*Enforcement:* test (`test_every_knob_is_documented_with_its_default`). The test checks the
default only when the call site passes a string literal. A computed default, such as
`CSP_POLICY`'s, is checked by name only and its row is kept accurate by review.

### 3. Switches and provider lists use the typed helpers

An on/off switch **MUST** be read with `knob_flag`. It treats only `false`, `0` and `no`
as off. Every other value, including `off`, an empty string and a typo, is on. That is
fail-closed only when "on" is the secure state, so a switch read with `knob_flag` **MUST** be
named for the secure behaviour, as `ORY_REQUIRE_AUDIENCE` is (`REQUIRE_`, `ENFORCE_`). It
**MUST NOT** be named for the insecure one (`SKIP_`, `DISABLE_`, `ALLOW_INSECURE_`), because
a mistyped value would then switch the check off. A comma-separated list of provider names
**MUST** be read with `knob_set`, which strips and lowercases the entries. Code **MUST NOT**
parse either shape by hand.

*Enforcement:* test (`test_knob_flag_switches_are_named_for_the_secure_state` rejects a
`knob_flag` name containing `SKIP`, `DISABLE`, `INSECURE`, `BYPASS` or `NO_VERIFY`).

## Outbound HTTP

### 4. HTTP clients come from `app.outbound`

Code in `app/` **MUST** build HTTP clients with `app.outbound.outbound_client`. It **MUST**
name upstream failures by their `app.outbound` aliases: `UpstreamStatusError` for a non-2xx
response and `UpstreamTransportError` for no response at all. No module in `app/` other than
`app/outbound.py` imports `httpx`. In particular, routers **MUST NOT** import `httpx`. See
[ADR-0006](adr/0006-outbound-http-and-logging-helpers.md).

A router function that calls Descope **MUST** catch both `UpstreamStatusError` and
`UpstreamTransportError` and answer them with 502.
The FGA and document routes call no provider: their store answers a `RelationshipStoreError` (§18). Otherwise a Descope outage surfaces as an unhandled 500. A
function that catches `Exception` counts as handling both.

The routes behind `DescopeManagementAdapter` (§5) leave that decision to the adapter, which
reports an upstream failure as an `Error`.

- Exception: the adapter's member methods (`list_members`, `invite_member`,
  `set_member_status`, `remove_member`), plus `update_profile_attribute`,
  `load_tenant_settings` and `update_tenant_settings`. They catch only `UpstreamStatusError`,
  so a transport failure propagates and the route answers 500 (the last three are called
  by `DescopeSyncAdapter`'s custom attributes, which answer any failure as `SyncError`, so
  the profile and tenant settings routes answer it 502). That predates #512, and
  changing those answers to 502 is a behaviour change left for a follow-up.
- Exception: the adapter's six access-key methods. None of them catches an upstream failure,
  so every Descope error and timeout on the `routers/accesskeys.py` routes surfaces as 500.
  That also predates #512 and is left for the same follow-up.

*Enforcement:* Ruff (TID251 on `httpx`), plus tests.
`test_router_functions_that_call_descope_handle_upstream_failures` checks each router
function that reads `descope_client` or calls a same-module helper that does.
`test_adapter_methods_that_call_descope_handle_upstream_failures` checks each
`DescopeManagementAdapter` method that calls its client, and keys the exceptions above by
method. `test_descope_management_adapter.py` pins what each excepted method does.

### 5. The Descope Management API is called through `DescopeManagementClient`

Calls to Descope's `/v1/mgmt/...` endpoints **MUST** go through
`app.services.descope.DescopeManagementClient`. Routers and dependencies use the
process-wide instance on `request.app.state.descope_client`. Code **MUST NOT** build a
management URL or an `Authorization: Bearer <project>:<key>` header itself.

`routers/users.py`, `routers/attributes.py`, `routers/accesskeys.py`, `routers/internal.py`
and `routers/protected.py` **MUST NOT** touch the client or an upstream error type. They
call `app.services.descope_management.DescopeManagementAdapter`, taken from
`app/dependencies/descope.py`. Each of its methods returns a `Result` carrying the API's own
response shape.

- Exception: `app/services/logout.py` (`_descope_management_logout`). It calls
  `/v1/mgmt/user/logout` with its own short-lived client, and that request is preserved
  byte-for-byte (NFR-8).

*Enforcement:* review, plus a test
(`test_pass_through_routers_reach_descope_only_through_the_adapter`).

### 6. Every outbound call has an explicit timeout

Every outbound HTTP call **MUST** carry a timeout chosen by its caller. `outbound_client`
makes `timeout` a required keyword-only argument, so a call site cannot fall back to an
implicit default.

- Exception: OIDC discovery and JWKS fetches made inside `py-identity-model`. The library
  builds its own client with its default timeout of 30 seconds. It reads no environment
  variable, and the backend passes it no client of its own.

*Enforcement:* the helper's signature, plus tests (`test_outbound.py`, including one that pins
`logout.py`'s `timeout=5.0`).

## Logging

### 7. Loggers come from `app.logbook`

Every module in `app/` that logs **MUST** create its logger at module level with
`logger = logbook(__name__)` (`from app.logbook import logbook`). It **MUST NOT** call
`logging.getLogger`.

Handlers, formatters and levels **MUST** be configured only in
`app.logging_config.setup_logging`. That function also quiets the `httpx`, `httpcore` and
`uvicorn.access` loggers.

- Exception: `app/logbook.py` itself.

*Enforcement:* Ruff (TID251 on `logging.getLogger`).

### 8. Log calls pass arguments, not formatted strings

Log calls **MUST** pass values as `%`-style arguments, as in
`logger.warning("sync failed for %s", user_id)`. They **MUST NOT** pass an f-string or a
`.format()` result, and they **MUST NOT** use `logger.warn`.

*Enforcement:* Ruff (G004, G010).

### 9. No secrets in logs

Log records **MUST NOT** contain credentials. That covers bearer tokens, the Descope
management key, `DESCOPE_WEBHOOK_SECRET`, `DESCOPE_FLOW_SYNC_SECRET`,
`INTERNAL_IDENTITY_KEY`, `ZITADEL_ADMIN_TOKEN` and `KEYCLOAK_ADMIN_CLIENT_SECRET` and the
service-account tokens they are exchanged for, and raw `Authorization` headers. Log identifiers instead, such as a
user id, tenant id, provider name or HTTP status. An upstream response body **MUST** be
truncated to 500 characters before it is logged (`exc.response.text[:500]`).

The same applies to spans. `app.telemetry._sanitize_httpx_request` overwrites one span
attribute, `http.request.header.authorization`, with `[REDACTED]`, and does nothing else. It
is not a general header filter. Outbound Descope calls carry the management key in their
`Authorization` header. Inbound requests carry callers' bearer tokens, and the
`/api/internal/*` endpoints receive shared secrets in `X-Identity-Key`, `X-Flow-Secret` and
`X-Descope-Webhook-S256`. A deployment that exports telemetry therefore **MUST NOT** set any
`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*` variable: `_CLIENT_REQUEST`, `_CLIENT_RESPONSE`,
`_SERVER_REQUEST` or `_SERVER_RESPONSE`. The OpenTelemetry httpx and FastAPI instrumentors read
those variables themselves and record every matching header as a span attribute. Nothing in
the backend redacts inbound headers.

*Enforcement:* review.

## Results and errors

### 10. Services return `Result[T, IdentityError]`

A public method of a `*Service` class in `app/services/` **MUST** return
`Result[T, IdentityError]` (from `expression`). It **MUST** report a domain outcome, such as
not found, a conflict, invalid input or a provider failure, as `Error(...)`. It **MUST NOT**
raise for one. See [ADR-0001](adr/0001-services-return-result.md).

- Exception: `SyncStatusService.record_event`, which appends inside the caller's
  transaction.
- Exception: `UserService.list_canonical_users`, an unfiltered repository read with no
  domain error.

*Enforcement:* test (`test_service_methods_return_result`).

### 11. Services and repositories do not know about HTTP

Modules in `app/services/` and `app/repositories/` **MUST NOT** import `fastapi` or
`starlette`. In particular, they **MUST NOT** raise `HTTPException`.

- Exception: `app/services/logout.py`. The RP-initiated logout strategy raises
  `HTTPException(500)` when the provider advertises no `end_session_endpoint`.

*Enforcement:* test (`test_services_and_repositories_do_not_import_fastapi`).

### 12. Routers answer a `Result` only through `result_to_response`

A route handler that holds a `Result` **MUST** turn it into the HTTP response with
`app.errors.problem_detail.result_to_response`, for both the `Ok` and the `Error` arm. The
handler **MAY** branch on the result first. It **MAY** also reshape an `Ok` value before
handing it over, as in `Ok({"roles": result.ok})`, and pick the success status with
`status=`. It **MUST NOT** turn an `Error` into an `HTTPException` or a hand-built response.

RFC 9457 problem-detail bodies (`application/problem+json`) **MUST** be built only in
`app/errors/problem_detail.py`.

- Exception: `GET /api/identity` (`routers/protected.py::canonical_identity`). Its
  400/404/409/500 answers predate the canonical service layer and are part of the
  endpoint's published contract. The first-login refusals of a provider whose
  organization picks the tenant (Zitadel) answer in the same `{"detail": ...}` style:
  403 for no organization, an organization linked to no single active tenant, or a
  UserInfo `sub` that differs, 409 for an email a canonical user already holds, 500
  when the provider has no `providers` row, and 503 when UserInfo cannot be read, as
  for every provider fault in the module.
- Exception: the handlers in `routers/users.py` and `routers/attributes.py`. They answer an `Error` from
  `DescopeManagementAdapter` (or, in `attributes.py`, a `SyncError` from the sync adapter's
  custom attributes) with the `{"detail": ...}` 400/403/502 bodies those routes had
  before the adapter existed, and `get_profile` answers one with no custom attributes and
  `get_tenant_settings` a tenant the provider lacks with blank defaults. The member activate and deactivate routes answer a member shared with
  another tenant with a 409 in the same style (#581). Moving them to problem details is a
  behaviour change left for a follow-up.

*Enforcement:* review.

### 13. A new error kind is an `IdentityError` with a mapping

A new domain error kind **MUST** be a `@dataclass(frozen=True)` subclass of
`app.errors.identity.IdentityError` defined in that module. It **MUST** be registered in
`_ERROR_TYPE_MAP` in `app/errors/problem_detail.py` with its problem-type URI, HTTP status
and title. A kind whose message may carry upstream detail **MUST** also have an entry in
`_SANITIZED_DETAIL`.

*Enforcement:* test (`test_every_identity_error_is_a_registered_frozen_dataclass`).

### 14. Guards refuse by raising

A routing-layer guard, meaning a helper that decides whether a request may continue,
**MUST** refuse by raising. It **MUST NOT** return a response or an error value that its
caller has to remember to propagate. A guard that refuses with a domain error **SHOULD**
raise `IdentityErrorRaised(<IdentityError>)`, which the app renders through
`result_to_response`, so the body matches every other problem detail. See
[ADR-0004](adr/0004-guards-raise-identity-errors.md).

*Enforcement:* review.

## Authorization and tenancy

### 15. Every tenant-scoped route declares its authorization

A route is tenant-scoped when it depends on `get_tenant_id`, `get_tenant_access`, `get_tenant_claims`
or `get_tenant_memberships`. Every
tenant-scoped route **MUST** also depend on `require_role`, `require_permission` or
`require_fga`. See [ADR-0003](adr/0003-authorization-as-route-dependencies.md).

- Exception: a route any member of the caller's tenant may call, where membership itself is
  the authorization decision. Each such route **MUST** be listed in `MEMBER_ROUTES` in
  `backend/tests/unit/test_router_authorization.py` with its reason. Adding an entry is a
  reviewed decision. The register currently holds `GET /api/tenants`,
  `GET /api/tenants/current`, `GET` and `POST /api/tenants/{tenant_id}/resources`,
  `GET /api/roles/me`, `GET /api/tenants/current/settings`, `POST /api/documents` and
  `GET /api/documents`.

*Enforcement:* test (`test_router_authorization.py`). The test walks every route in
`app.main.app` and also fails on a stale register entry.

### 16. Router code takes identity from dependencies, not `request.state`

Code in `app/routers/` **MUST NOT** read `request.state.claims` or
`request.state.principal`. It takes them from the dependencies in `app/dependencies/`:
`get_claims`, `get_current_user`, `get_tenant_id`, `get_tenant_access` (the tenant with the
caller's roles and permissions in it), `get_tenant_claims` and `get_tenant_memberships`. In
particular, a
handler **MUST NOT** read the `dct` claim to find its tenant. It takes the tenant from
`get_tenant_id`.

- Exception: `routers/auth.py::logout`, which needs the principal or `None` so it can fall
  back to the first configured provider.
- Exception: `routers/protected.py::canonical_identity`, which checks principal, identity
  and claims together for one 401, and hands the request's provider to first-login
  provisioning.
- Exception: `routers/idp_links.py::_require_user_in_tenant`, a raising guard that answers
  404 rather than 401/403 so it does not reveal whether a user exists.

*Enforcement:* test (`test_router_code_does_not_read_request_state`).

### 17. Authorization fails closed

An authorization dependency **MUST** refuse (401 or 403) on every branch where it cannot
reach an unambiguous decision. That includes a missing subject, missing claims, no tenant
or several candidate tenants, a tenant selector that names a tenant the caller does not
belong to, a non-active canonical user and a malformed `dct`. An error from the FGA store
**MUST** be answered with 502 and treated as a denial, never as an allow.

*Enforcement:* review, plus tests (`test_rbac_dependencies.py`, `test_tenant_resolution.py`,
`test_fga_dependencies.py`, `test_keycloak_canonical_authorization.py`).

### 18. One resolver decides the tenant and the grants

`get_tenant_id`, `get_tenant_access`, `require_role`, `require_global_role` and `require_permission` **MUST** take the request's tenant
and the caller's roles and permissions from
`app.dependencies.tenant_access.resolve_tenant_access`. A dependency **MUST NOT** read a
tenant, role or permission claim itself. The resolver takes its sources in this order:

1. **The provider's claims extractor** (`ProviderTokenConfig.tenant_claims`, §28), for a
   provider that is not listed in `CANONICAL_RBAC_PROVIDERS`. Descope's extractor reads
   the signed `dct` and `tenants` claims, and a Descope principal **MUST** be decided from
   them alone. Descope is the one provider with an extractor. A provider with no extractor
   has no source and is refused: another provider's claim shapes on its token are not read.
2. **The request's tenant selector**, for a provider listed in `CANONICAL_RBAC_PROVIDERS`
   (default `ory`). The selector is the `X-Tenant-ID` request header. It grants nothing by
   itself: it only chooses among the tenants the caller's canonical memberships name. A
   selector naming any other tenant **MUST** be refused, and no grant is read from that
   tenant. An empty or repeated header is refused too. A value that parses as a UUID is
   compared in its canonical lower-case form, and any other value as it is. The SPA
   remembers the tenant chosen last, and each new sign-in drops it, for every provider: the
   selector then starts on the membership whose `external_org_id` (`GET /api/identity`) is
   the organization the sign-in named, else the first membership.
3. **The caller's sole canonical membership**, when the request selects nothing.

A canonical tenant whose status is not `active` (`suspended`) **MUST NOT** be served to its
members: the resolver refuses a request for it (403, `TenantRefusal.NOT_ACTIVE`), it is no
sole membership, `get_tenant_memberships` and the SPA's selector leave it out, and the
`/api/tenants/{tenant_id}/resources` routes refuse it the same way (#689). A tenant whose
status was not read is not served either. On a route whose state belongs to no tenant
(`require_global_role`) the membership still counts: that state is not the suspended
tenant's, so an operator keeps it. `TenantResource` rows carry their tenant's id space
(`TenantIdSpace`): a canonical principal's resources are read and written as canonical, a
Descope principal's as Descope, so a Descope tenant created with a canonical tenant's UUID
as its id names none of that tenant's resources (#690).

Anything else is 403, including an extractor that raises, and one whose answer is not a
`TenantClaims` with a non-empty string for the tenant and lists of strings for the roles
and the permissions. Roles and permissions **MUST** be resolved from the canonical model
only for a provider listed in `CANONICAL_RBAC_PROVIDERS`. Ory, Zitadel and Keycloak are
authorized this way ([ADR-0011](adr/0011-keycloak-canonical-authorization.md)): their
tokens say who the caller is, and no request-time decision reads a role, permission or
tenant claim from them.

The canonical membership read of the third source **MUST NOT** be served from the identity
cache, and it **MUST NOT** be kept beyond the request that made it. `get_tenant_id` **MUST NOT** open a database session or read the canonical
model for any other provider. See
[ADR-0009](adr/0009-provider-registrations-and-one-tenant-resolver.md).

**A route the SPA calls serves every provider** ([ADR-0012](adr/0012-routes-speak-canonical.md)).
It reads and writes canonical services, and a provider is called only from a sync adapter or a
relationship store. Each route says how it keeps one provider's tenant and user ids from another:
every route that depends on `get_tenant_id` or `get_tenant_access` **MUST** be listed in one
register of `backend/tests/unit/test_descope_backed_routes.py`, which walks every route of the
application against them:

| Register | Routes | What keeps the ids apart |
|---|---|---|
| `DESCOPE_TENANT_ROUTES`, `DESCOPE_PROJECT_ROUTES` | None today | `require_descope_principal` |
| `SYNC_PROVIDER_TENANT_ROUTES`, `SYNC_PROVIDER_PROJECT_ROUTES` | Role assignment, members, role and permission definitions | `require_sync_provider_principal` |
| `PER_PROVIDER_ROUTES` | `/api/keys` | The sync provider builds the service |
| `RELATIONSHIP_STORE_ROUTES` | `/api/fga/*`, `/api/documents*` | `capabilities.served` |
| `ADAPTER_CAPABILITY_ROUTES` | The profile's custom attributes, the tenant settings | `capabilities.served` |
| `PROVIDER_NEUTRAL_ROUTES` | `GET /api/tenants/current`, `GET /api/roles/me`, the two `/api/access-checks` permission routes | Nothing behind them hands an id to a provider |

Adding a route to `PROVIDER_NEUTRAL_ROUTES` is a reviewed decision, with its reason.
`GET /api/roles/me` answers the caller's own roles and permissions in the resolver's tenant: a
Descope principal's from its claims, a canonical principal's from the canonical model.
`GET /api/tenants/current` reads a canonical principal's tenant from the canonical model and
loads only a Descope principal's (its `dct`) from Descope; it refuses any other principal (403)
(#673). The `/api/access-checks` routes answer the resolver's tenant to a holder of
`documents:read` or `settings:manage` and depend on nothing but the resolver.

**The Descope guard.** A route that hands the request's tenant id or the caller's `sub` to
Descope's Management API **MUST** depend on `app.dependencies.descope.require_descope_principal`,
on the route or on its router, and be listed in `DESCOPE_TENANT_ROUTES`, or in
`DESCOPE_PROJECT_ROUTES` when it is over state that belongs to the whole Descope project. The
resolver gives a principal of a canonical provider a canonical tenant id, which names nothing in
Descope, so the guard refuses (403) every principal whose provider is not `descope_managed`
(§28), before the tenant is resolved and before Descope is called. Its 403 body is its own,
`This route serves Descope principals only`, not the resolver's "select a tenant first":
selecting a tenant would not help the caller. A request with no claims gets 401 from the guard
itself (§17). A route over project state needs the guard because `require_role` answers for one
tenant: without it, a canonical principal who is an admin of any one of several tenants would
reach that state by naming that tenant. A route that is handed the management adapter
(`get_descope_management`) **MUST** carry the guard or be listed, with what keeps another
provider's subject from Descope, in `UNGUARDED_ADAPTER_ROUTES`. No route is in these registers
today; code that builds a Descope service from the caller's ids calls the guard itself (the
access keys, below).

**Routes whose writes go to the sync provider.** A route whose service pushes its writes to the
configured sync adapter (`SYNC_PROVIDER`) **MUST** depend on
`app.dependencies.sync_provider.require_sync_provider_principal` and be listed in
`SYNC_PROVIDER_TENANT_ROUTES` or `SYNC_PROVIDER_PROJECT_ROUTES`: the role assignment routes
(`POST /api/roles/assign`, `/api/roles/remove`), the member routes (`GET /api/members`,
`POST /api/members/invite`, `POST /api/members/{user_id}/deactivate` and `/activate`,
`DELETE /api/members/{user_id}`) and the role and permission definition routes (`/api/roles`,
`/api/roles/{name}`, `/api/permissions`, `/api/permissions/{name}`; the two lists take the
request's tenant and are in `SYNC_PROVIDER_TENANT_ROUTES`). The adapter takes the
request's tenant id and the user ids as its provider's, so the guard refuses (403, `This route
serves <provider> principals only`) every principal whose provider is not the sync provider,
before the tenant is resolved and before anything is written. In a Descope deployment
(`SYNC_PROVIDER=descope`, the default) it passes exactly the `descope_managed` principals
`require_descope_principal` passes, with the same body (#676). Who is a principal of the sync
provider **MUST** be decided by `identity.is_sync_provider_principal` alone: the guard calls it,
and `capabilities.served` reports it as `sync_provider_principal` in `GET /api/capabilities`, by
which the SPA shows the Members and Roles pages (`PAGE_REQUIREMENTS`), so no page the SPA shows
calls a route that fences the caller out. Each of these routes **MUST** also depend on
`app.dependencies.rbac.refuse_machine_principal` (below): no machine credential changes which
people hold which roles, the definitions every tenant's grants read, or who belongs to a tenant
(#683).

- **Members.** The member routes take their members from
  `app.dependencies.identity.get_member_directory`: in a Descope deployment the Descope
  management adapter, as before, and otherwise `MemberService` over the canonical users and
  memberships, whose changes the sync adapter pushes (Keycloak: organization membership and the
  role's organization group) (#677). They stay `require_role("owner", "admin")` in the request's
  tenant. An invitation **MUST** only create: the user and its memberships commit together, and
  an email any canonical user already has, in any case, is refused (409,
  `MemberRefusal.ACCOUNT_EXISTS`) whatever tenant holds it, so an admin can neither enrol another
  tenant's account nor learn about it. A member's status is the account's (the model keeps no
  status per membership), so it is set only for a user whose sole tenant is the request's, under
  a lock on the user's row (409 otherwise), and a user who is no member of the tenant reads the
  same whether or not it exists. Deactivating, activating and removing a member **MUST** first
  pass `access.act_on_member` over the member's roles in the tenant, read from the same directory
  (Descope's `userTenants` or the canonical assignments): a member holding `owner` only by an
  owner (403, `MemberActionRefusal.OWNER_REQUIRED`), one holding `operator` only by an operator
  (`operator_grant_check`), and never the caller itself for a deactivation or a removal (403,
  `MemberActionRefusal.ACTING_ON_SELF`), since a deactivated user cannot sign back in to undo it.
- **Role assignment.** `POST /api/roles/assign` **MUST** give a role only to a user who already
  holds one in the request's tenant, whatever the adapter (403, `access.NotATenantMember`), with
  the same answer for an unknown user and another tenant's: otherwise a tenant admin could pull
  another tenant's user, an operator among them, into theirs (#693). The first membership comes
  from sign-in provisioning or an invitation.
- **Definitions.** The role and permission definitions belong to no tenant and every tenant's
  grants read them, so the routes that change them are gated by
  `app.dependencies.rbac.require_definitions_role`
  (`require_global_role("operator", claimed_roles=("owner", "admin"))`): a canonical principal
  **MUST** hold `operator` by its sole membership. A tenant `admin` is refused: its role is one
  tenant's, and with it the caller could delete the `owner` role, which ends that role in every
  tenant, or grant the `member` role a permission everywhere. A Descope principal is decided from
  its claims and keeps `owner` or `admin`, as before. Listing them (`GET /api/roles`,
  `GET /api/permissions`) changes nothing and shows only the global definitions, so it is gated by
  `require_role(*DEFINITION_READERS)` (`owner`, `admin` or `operator`) in the request's tenant: the
  Roles page lists them to assign a role (#707).
- **`operator` itself.** Assigning or removing `operator` through `POST /api/roles/assign` or
  `/api/roles/remove`, or inviting with it through `POST /api/members/invite`, **MUST** be
  refused (403, `RoleGrantRefusal.OPERATOR_GRANT`) unless the caller holds `operator` as
  `require_global_role` decides it (`app.dependencies.rbac.operator_grant_check`): the routes take
  `owner` or `admin` in one tenant, and `operator` opens every tenant's state. Creating, changing,
  renaming to or from, or deleting the `operator` role definition **MUST** be refused the same
  way (`RoleGrantRefusal.OPERATOR_DEFINITION`, `operator_definition_check`): a Descope `owner` or
  `admin` passes `require_definitions_role` and could otherwise strip `operator` from every
  operator. Both hold for every provider, Descope included.

**Machine credentials.** The `/api/keys` routes serve the `machine_credentials` capability
through `MachineCredentialService`, which the configured sync provider builds
(`get_machine_credential_service`): a Descope deployment's Descope access keys, and a canonical
provider's canonical machine credentials (Keycloak: a client the adapter registers). They carry
no static guard, and are listed in `PER_PROVIDER_ROUTES`: the Descope builder **MUST** call
`require_descope_principal` before it builds anything, and each route **MUST** take its service
before its tenant, so another provider's principal is refused before the tenant is resolved and
before Descope is called. A token issued to a canonical provider's machine credential client
(`ProviderTokenConfig.machine_client`) **MUST** be authorized from that credential alone
(`rbac._load_machine_memberships`): its one tenant, its roles there, and nothing while it is
inactive, expired or deleted. It is never resolved as a user. A credential **MUST** hold at least
one role, only roles its creator holds in the tenant, and never `operator`
(`ROLES_NOT_FOR_MACHINES` in `app/domain/credentials`): a credential has exactly one membership,
so `require_global_role` would serve it every tenant's state. Such a token **MUST** be refused
(`refuse_machine_principal`, 403 `MachinePrincipalRefused`) on every route that changes
people's roles, memberships, credentials or relations: the `/api/keys` routes, so no credential
mints its successor; every route of the sync provider's registers above; the relationship
store's writes (`PUT /api/fga/schema`, `POST` and `DELETE /api/fga/relations`, a document's
share and revoke), so no credential makes anyone a document's owner; and the IdP-link writes,
which decide which account signs in as a person. `MACHINE_REFUSED_ROUTES` in
`test_descope_backed_routes.py` lists them, and every other write of the application **MUST** be
listed in `MACHINE_PERMITTED_WRITES` there with why a machine may make it. A Descope access-key
session is not such a token: Descope has no `machine_client` reader (#699).

**Capabilities.** A route that serves an optional capability of the sync adapter (`Capability`
in `app/services/adapters/base.py`: the machine credentials, the custom attributes of the profile
and the tenant settings) or the relationship store (the FGA and document routes) **MUST** depend
on that capability's dependency in `app/dependencies/capabilities.py` as its first dependency,
before the guard. What serves the caller is decided by one function, `capabilities.served`: the
sync adapter's capabilities serve only a principal of the sync provider (its registration's
`own_principal` in `SYNC_ADAPTERS`; for Descope, a `descope_managed` provider), because a
capability takes the caller's `sub` and the request's tenant as that provider's own ids. The
exception is an adapter's `canonical_capabilities`, kept in the canonical model and taking none
of the provider's ids, which serve every principal authorized from the canonical model
(`rbac.is_canonical_principal`) and never a Descope principal, whose `dct` may name a canonical
tenant's id: Keycloak's machine credentials (#680). Each
relationship store serves only the principals whose ids it keys on (below). A caller not served
answers 404 (`CapabilityMissing`), not the guard's 403: the route is not there for them.
`GET /api/capabilities` reports `served`, and the SPA shows a page only when it is there. A route
that reaches the provider only through a sync adapter capability (the profile's custom
attributes, `GET` and `PATCH /api/tenants/current/settings`) carries no Descope guard and calls
the adapter, never `get_descope_management`; it is listed with its reason in
`ADAPTER_CAPABILITY_ROUTES`. Adding a capability to an adapter means implementing its interface
(`CustomAttributes` for `custom_attributes`; `IdentityProviderAdapter.__init_subclass__` refuses
a declaration without it).

**The relationship store.** The FGA and document routes reach relations only through the
`RelationshipStore` interface (`app/services/relationship_store.py`, from
`get_relationship_store`), which `RELATIONSHIP_STORE` selects, and default to the sync provider's
store (Descope's for Descope, OpenFGA for Keycloak and Zitadel). Only that module calls the
Descope client's FGA methods (`tests/unit/test_relationship_store.py`). Each store is one entry in
`app.dependencies.identity.RELATIONSHIP_STORES`, which says whom it serves, how it is built and
which user directory names its subjects. A store **MUST** serve only the principals whose ids it
keys on: Descope's store a `descope_managed` principal, keyed on `dct` and `sub`; OpenFGA a
principal of a provider in `CANONICAL_RBAC_PROVIDERS`, keyed on the resolver's canonical tenant
id and the caller's canonical user id, never the token's `sub`. So these routes carry no
`require_descope_principal`: `served` refuses everyone else 404. They take the tenant from the
resolver (`get_tenant_id`) and the subject from `app.dependencies.fga.get_relationship_caller`
(`require_fga` resolves both the same way), and look a target user up in the store's own
directory (`get_user_directory`: Descope's users or the canonical ones), never in another
provider's. A store reports every failure as a `RelationshipStoreError` whose `kind` decides the
answer (`store_error_response`).

**State that belongs to no tenant.** A route over state that belongs to no tenant and that
Descope does not hold **MUST** be gated by `app.dependencies.rbac.require_global_role`, not
`require_role`, and be listed in `GLOBAL_ROLE_ROUTES`: `GET /api/users` (every tenant's users),
`/api/providers`, `/api/sync/status` and `/api/events/recent`, and the changes to the role and
permission definitions above (`require_definitions_role`). One role in one tenant opens all of that state,
so on these routes the request chooses nothing. A canonical principal is decided by their sole
membership, and one with several memberships is refused, whether they send the `X-Tenant-ID`
header, carry a `dct` claim on the token, or name no tenant at all. A Descope principal is
decided from its claims, as by `require_role`. The project-level grant these routes need is
#593. A route gated by `require_role` or `require_permission` **MUST** depend on `get_tenant_id`
or on a guard, or be listed with its reason in `SELF_SCOPED_ROLE_ROUTES` in the same test file.

A `descope_managed` provider **MUST NOT** be listed in `CANONICAL_RBAC_PROVIDERS`: it
would pass the guard with a canonical tenant id. The middleware factory refuses that
configuration at startup (`refuse_canonical_descope_managed`).

- Exception: a `dct` claim on the token of a provider listed in
  `CANONICAL_RBAC_PROVIDERS` is read as a tenant selector, as it was before the header
  existed. It is put in the same spelling as the header and held to the same check, and a
  `dct` that disagrees with the header is refused. On a `require_global_role` route it
  chooses nothing, like the header.
- Exception: a request that no middleware attributed to a provider, which only a test
  that sets `request.state.claims` by hand can make, is read with Descope's extractor by
  the resolver, which logs a warning each time. `require_descope_principal` refuses it: the guard passes only a provider
  it knows to be `descope_managed`. Code in `app/` that sets `request.state.claims`
  **MUST** set `request.state.provider` beside it.
- Exception: `get_tenant_memberships` (`dependencies/tenant.py`) gives every tenant the
  caller belongs to, for the routes that list them or check membership of a path tenant
  (`GET /api/tenants`, the `/api/tenants/{tenant_id}/resources` routes). It takes them from
  the resolver's sources: a provider in `CANONICAL_RBAC_PROVIDERS` gets its canonical
  memberships (the same uncached, per-request read as source 3), and any other principal
  the `tenants` claim through `get_tenant_claims`. A Descope principal's tenant ids are
  Descope's, which name no canonical tenant, so its memberships are its claim (#673).
- Exception: `get_tenant_claims` returns the Descope `tenants` claim as it is, for
  `get_tenant_memberships`. It reads the claim
  through `get_descope_claims`, which refuses (403, `DESCOPE_CLAIMS_ONLY`) every principal
  whose provider is not `descope_managed`, whatever its token carries: a provider mapper
  that put a `tenants` claim on Ory, Zitadel or Keycloak tokens would otherwise grant
  membership of any tenant it names.
- Exception: `POST /api/tenants` is gated by `require_admin_role`
  (`routers/tenants.py`), which reads a top-level `roles` claim itself and takes no
  tenant. It takes the claims from `get_descope_claims`, so only a Descope principal is
  decided from that claim; every other provider's is refused (403) (#596).
- Exception: `GET` and `PATCH /api/profile` carry no capability gate, because every
  provider's session loads and saves it. Names and email are the canonical user's
  (`GET /api/identity`'s resolution); `PATCH` saves the caller's own given and family
  names there (never the email: sign-in and sync link accounts by it; both names are
  required and held to Keycloak's person-name rules, which every sync provider takes,
  so a name a provider would refuse is a 422, not a save the next pull undoes) and pushes them,
  with `OWN_PROFILE_OF`, only to the caller's own account at the sync provider, and only
  when the caller is its principal (`capabilities.sync_provider_subject`). The custom
  attributes are asked of the sync adapter only for a caller its `custom_attributes`
  capability serves, and are empty for anyone else; they are written through
  `PATCH /api/profile/attributes`, which is gated.
- Exception: `GET /api/tenants` takes no tenant from `get_tenant_id` and carries no guard:
  it lists the caller's own memberships from `get_tenant_memberships` (#673).
- Exception: the FGA schema belongs to no tenant and is gated by `require_role`, which
  answers for one tenant. On OpenFGA the schema is read-only: `infra/openfga` holds the model,
  and `PUT /api/fga/schema` answers 409. A Descope principal who
  holds the role in their current tenant is served there, as before the resolver (#593).
- Exception: the `/api/users/{user_id}/idp-links` routes are gated by `require_role` and
  take no tenant from `get_tenant_id`. `_require_user_in_tenant` reads `dct` itself and
  answers 404 without one (#590).

*Enforcement:* review, plus tests (`test_tenant_resolution.py`, `test_rbac_dependencies.py`,
`test_tenant_dependencies.py`, `test_descope_claim_routes.py`, which sends Keycloak, Ory
and Zitadel tokens with forged `tenants` and `roles` claims to every route that reads them
and to the tenant routes, which must not read them, `test_tenant_routes_canonical.py`,
which serves each provider's principal its own tenants and resources and refuses another
tenant's, `test_capabilities.py`, which holds each capability to its routes and its
dependency first on each, `test_machine_principal.py`, which shows a machine token gets its
credential's grants and nothing else and is refused on every `/api/keys`, role, member,
relation-write and IdP-link-write route, and `test_descope_backed_routes.py`, which walks every route
of the application against its registers, calls each guarded route as a canonical
principal, and calls each `require_global_role` route as an operator of one of several
tenants who names it). `test_keycloak_canonical_authorization.py` shows a Keycloak
principal's grants come from the canonical store whatever its token claims, and fails on
a Keycloak branch in `app/dependencies/` or `app/routers/`. `test_provider_registration.py`
fails on a writer of `request.state.claims` that records no provider.

## Data access

### 19. Queries live in `app/repositories/`

SQL statements against the canonical model **MUST** be built in `app/repositories/`, and
services **MUST NOT** build them. Routers and dependencies **SHOULD NOT** build statements
either. They **SHOULD** construct a repository, as `routers/idp_links.py` does.

- Exception: `routers/tenants.py`, which reads and writes the demo `TenantResource` table.
- Exception: `routers/documents.py`, which reads and writes the demo `Document` table.
- Exception: `dependencies/identity.py`, which takes the reconciliation advisory lock
  (`pg_advisory_xact_lock`).
- Exception: `main.py`, whose lifespan runs `SELECT 1` to check the database connection
  before it accepts requests.

*Enforcement:* test (`test_queries_live_in_repositories`).

## Tracing

### 20. Every public service method opens its own span

Every module in `app/services/` that spans **MUST** create its tracer at module level with
`tracer = trace.get_tracer(__name__)` (`from opentelemetry import trace`). Every public
method of a class in `app/services/` **MUST** open a span named `<Class>.<method>` with
`with tracer.start_as_current_span("<Class>.<method>"):` around its body, for example
`UserService.create_user` or `DescopeManagementClient.load_tenant`. A public module-level
function **MUST** open a span named `<module>.<function>`. The logout strategies, which are
private functions reached through `LOGOUT_STRATEGIES`, **MUST** open a span named
`logout.<logout_kind>` (`logout.management`, `logout.rp_initiated`). A method **MAY** set
attributes on its span. Attributes are identifiers and counts, never credentials (§9). A
method **MAY** return before opening its span when it has nothing to do, as
`CacheInvalidationPublisher.publish` does when Redis is not configured.

`DescopeManagementClient` spans **MUST** pass `record_exception=False` and
`set_status_on_exception=False`. Callers routinely treat an `UpstreamStatusError` as an
answer, such as a 404 from `load_user` or a 400/404 from `delete_relation` during a revoke.
Whether the call failed is therefore the caller's decision, and the httpx client span
beneath records the HTTP status. Service spans keep OpenTelemetry's defaults. See
[ADR-0007](adr/0007-a-span-per-service-method.md).

- Exception: abstract methods, such as those of the `IdentityService` interface in
  `services/identity.py`. A concrete implementation is traced.
- Exception: `DescopeManagementClient.close`, a lifecycle method that closes an *injected*
  HTTP client. Nothing in `app/` calls it, because `main.lifespan` owns the shared client and
  closes it.
- Exception: `canonical_user` and `canonical_tenant` in `services/canonical.py`, which are
  pure dict-shaping helpers with no I/O.
- Exception: `run_cache_invalidation_subscriber` in `services/identity_resolution.py`, a
  background loop for the life of the process whose span would never end.
- Exception: the provider sync adapters in `services/adapters/`. They keep their own
  `<provider>.<operation>` span names (`descope.sync_user`, `ory.sync_tenant`) and span
  only the operations that reach the provider.

*Enforcement:* test (`test_public_service_methods_open_their_own_span`,
`test_logout_strategies_open_a_span_per_logout_kind`,
`test_descope_client_spans_leave_status_to_the_caller`).

## Changing the rules

### 21. A rule change is recorded

A change that adds, removes or reverses a **MUST** rule **SHOULD** come with an ADR in
[`docs/adr/`](adr/README.md). An ADR that replaces an earlier one marks the earlier one
`Superseded by ADR-NNNN` and leaves it in place. The same change **MUST** update this file,
its enforcement and any named exceptions together, so the rules keep describing the code.
A new exception **MUST** be named in the rule it breaks.

*Enforcement:* review.

## Layers and the domain

The architecture, its layer map, the context map and each context's glossary are in
[ADR-0008](adr/0008-layered-architecture-with-a-functional-domain-core.md).

### 22. Imports point inward

The layers are, from the outside in: `app/routers/`, `app/dependencies/`, `app/services/`,
`app/repositories/`, `app/models/` and `app/domain/`. A module **MUST NOT** import a layer
that comes before its own in that list.

*Enforcement:* import-linter (`layers`).

### 23. The domain is pure

Code in `app/domain/` **MUST** import only the standard library, `expression` and its own
context. It **MUST NOT** import SQLModel, SQLAlchemy, FastAPI, Pydantic, httpx, `logging`,
OpenTelemetry or any other package under `app/`. It **MUST NOT** do I/O, read the clock or
generate ids. The caller passes the time and new ids in, as `now=` and `role_id=` are
passed to `access.create_role`.

*Enforcement:* import-linter (`forbidden`), plus a test (`test_domain_is_pure`). The test
rejects an import outside the standard library and `expression`, a standard-library module
that does I/O (`os`, `time`, `random`, `logging` and the rest of `_IMPURE_STDLIB`), and any
use of `now`, `utcnow`, `today` or a `uuid` generator.

### 24. Domain functions return `Result`

A domain function **MUST** report an expected failure as `Error(<context error>)` and
**MUST NOT** raise for one. A context's errors **MUST** be `@dataclass(frozen=True)`
subclasses of its one error base, such as `AccessError`, and carry data, not a message.
Each **MUST** have a translation to an `IdentityError` in `app/errors/<context>.py`, which
is where the message a caller sees is written.

A decision function takes the state it needs and a command, and returns the new value
together with the domain event that records it:
`(state, command, *, ids, now) -> Result[(value, event), error]`. State that does not exist
is passed as `None`, and the decision answers with the context's not-found error.

*Enforcement:* test (`test_domain_does_not_raise`, which rejects any `raise` in
`app/domain/`, and `test_every_access_error_is_translated`), plus review.

### 25. Domain types are immutable

Every class in `app/domain/` **MUST** be a `@dataclass(frozen=True)` or an `Enum`. Changing
state means returning a new value, with `dataclasses.replace`. A value object with an
invariant **MUST** be built from outside input through its smart constructor, which returns
`Result`, as `access.role_name` does. A repository rebuilding a stored value **MAY** call
the type directly.

*Enforcement:* test (`test_domain_types_are_frozen_dataclasses_or_enums`).

### 26. Routers and services skip no layer

A module in `app/routers/` **MUST NOT** import `app.repositories`, `app.models` or
`app.outbound`. It reaches data and providers through one application service.

A module in `app/services/` **MUST NOT** import `app.models`. It holds domain values and
decides nothing itself: it loads state, calls a decision function in `app/domain/`,
persists the result, then invalidates caches and syncs. Rows are mapped to and from domain
values by functions in the repository's module, such as `role_from_row`, `role_to_row` and
`apply_role` in `app/repositories/role.py`.

The modules of a bounded context that has not moved onto the domain are exempt until its
migration, one entry per context:

- Exception: Directory (#515): `routers/canonical_users.py`, `routers/tenants.py`,
  `services/tenant.py`, `services/user.py`.
- Exception: Federation (#516): `routers/idp_links.py`, `routers/providers.py`,
  `services/identity_provisioning.py`, `services/idp_link.py`, `services/provider.py`.
- Exception: Documents and fine-grained authorization (#517): `routers/documents.py`,
  `routers/fga.py`.
- Exception: Synchronisation (#518): `routers/sync_status.py`, `services/inbound_sync.py`,
  `services/reconciliation.py`, `services/sync_status.py`.

§4's requirement that a router catch `UpstreamStatusError` and `UpstreamTransportError`
applies to the excepted routers for as long as they call Descope themselves.

*Enforcement:* import-linter (`context_layers`, defined in `backend/import_contracts.py`).
The exceptions are its `unmigrated` entries in `backend/pyproject.toml`. An entry that
names a module breaking no rule fails the contract.

### 27. Each bounded context has its own package and glossary

A bounded context **MUST** have its own package, `app/domain/<context>/`, and its own
glossary section in ADR-0008. Code in one context's package **MUST NOT** import another
context's package. A context refers to another's entities by id. Code outside
`app/domain/` **SHOULD** import a context's package, as in `from app.domain import access`,
not its modules.

*Enforcement:* import-linter (`independence`), plus review for the glossary.

## Providers

### 28. A provider is one registration

An OIDC provider **MUST** be added as one entry in `PROVIDER_REGISTRATIONS` in
`app/middleware/providers.py`: a function that reads the provider's own settings through
`app.knobs` and returns its `ProviderTokenConfig`, or `None` when the deployment does not
configure it. The middleware factory, both token middlewares and the logout router
**MUST** take their providers from `configured_providers()`, which walks that list and
takes no per-provider argument. They **MUST NOT** name a provider or read a provider's
settings themselves.

Everything that differs between providers is a field of `ProviderTokenConfig`: the
accepted issuers and audiences, the signing algorithms, the logout kind with the
provider's own client id and post-logout redirect URI, the claims extractor (§18),
`descope_managed` (§18), the token binding, the first-login organization reader and
whether the first-login profile comes from UserInfo, and `require_https`. `require_https` **MUST** default to on, so that plain-HTTP discovery is
allowed only for the one provider whose own switch turns it off. A registration that would
accept tokens it must not **MUST** raise, which stops startup. `configured_providers()`
raises `ProviderConfigurationError` when two providers share a name or an issuer, because
requests are matched to a provider by both. Two issuers that differ only in case or a
trailing slash count as shared.

A provider whose tokens need more than an issuer and an audience to be tokens for this API
declares a `token_binding`. Both token middlewares **MUST** check it (`binding_refusal`)
after the audience check and before any claim becomes a principal, a tenant or a
permission, so that it holds on every route, and **MUST** answer a refusal with the uniform
401.

The Keycloak registration **MUST** refuse to start with `KEYCLOAK_ISSUER_URL` set unless
`KEYCLOAK_AUDIENCE` names the API client and `KEYCLOAK_CLIENT_ID` the SPA client, and
**MUST** refuse `KEYCLOAK_REQUIRE_HTTPS` off unless the issuer host is loopback or a
`.localhost` name. That exemption is by name: nothing checks what a `.localhost` name
resolves to. The middleware factory therefore **MUST** log a warning at startup for every
provider whose `require_https` is off. Its binding is an RS256 signature, `aud`
containing the API client, `azp` the SPA client and `typ` `Bearer`: Keycloak signs the
SPA's ID tokens (`typ` `ID`) and refresh tokens (`typ` `Refresh`) with the same keys, and
its access tokens carry `sid` and `auth_time` too. It has no claims extractor: its
principals are decided from the canonical model. Its `organization` claim is read by
`keycloak_organization_alias` alone, and only first-login provisioning calls it, through
`ProviderTokenConfig.first_login_organization`: exactly one alias, the
`external_org_id` of one active tenant of the `keycloak` provider, places the user;
anything else provisions nothing. The profile comes from the access token. See
[ADR-0011](adr/0011-keycloak-canonical-authorization.md).

The Zitadel registration **MUST** refuse to start with `ZITADEL_ISSUER_URL` set unless
`ZITADEL_AUDIENCE` names the project id and `ZITADEL_CLIENT_ID` the SPA application, and
**MUST** refuse an `http` issuer while `ZITADEL_REQUIRE_HTTPS` is on, loopback included.
Its binding (`app/middleware/zitadel.py`) is the SPA's `client_id` and none of `azp`,
`at_hash`, `auth_time`, `sid` or `nonce`, because audience cannot tell the SPA's access
token from another application's or from an ID token. It has no claims extractor: its
principals are decided from the canonical model. Its organization claim is read by
`zitadel.organization_id` alone, and only first-login provisioning calls it, through
`ProviderTokenConfig.first_login_organization`. A provider with `profile_from_userinfo`
takes a first-login user's email and names from UserInfo, never from its token.
A first login whose organization picks its tenant **MUST NOT** be linked to an existing
canonical user by email, verified or not: an organization's administrators can set any
email on their users. It **MUST NOT** create a user for an email with more than one `@`, an empty
domain, a domain with an upper-case letter or ending in `.`, or whose domain, whole, is not one
of that tenant's `domains` (each read lower-cased, without spaces, a leading `@` or trailing dots; one that
reads as empty is dropped, and `domains` that are not a list are none). Either refusal is `Forbidden`,
the answer an unlinked organization gets, and nothing is written. The one answer hides which
refusal fired and anything about emails outside the tenant's domains; within those domains a
403 where a free email gets 200 still tells the organization's administrators who chose the
email that a canonical user holds it. A Descope reconciliation run **MUST NOT** change the
`domains` of a tenant linked to a provider's organization (`external_org_provider_id` set):
they are that organization's admission list, not the Descope tenant's of the same name.
The user such a login creates is marked `users.email_from_organization`, and no first
login of any provider (just-in-time provisioning, the Descope flow sync) **MUST** link to
that user by email either: it is `EmailLinkRefused` too. A Descope reconciliation run
**MUST NOT** link or update that user either; it skips it and logs the holder's id. A login that finds, by email,
the user a concurrent login for its own subject just created gets that user. A Zitadel or
Keycloak principal with no recorded provider config **MUST NOT** be provisioned (500).

Both token middlewares **MUST** build the principal and read the tenant with the
provider's extractor inside the code path that answers a failure with the uniform 401, so
that a principal that cannot be built or an extractor that raises is the same 401 in
standalone and in gateway mode, with the cause logged and not returned.

- Exception: `build_provider_configs` takes the shipped providers' settings as explicit
  arguments, for a middleware constructed by hand, which only tests do. It is the same
  assembly as `configured_providers()`: each provider is built by the function its
  registration calls, and an ambiguous list is refused the same way. The application does
  not call it. Both middlewares keep their `descope_project_id` and `ory_*` constructor
  arguments for those tests (#591).
- Exception: `app/services/logout.py::_descope_management_logout` reads
  `DESCOPE_PROJECT_ID` and `DESCOPE_MANAGEMENT_KEY` itself (§5).

*Enforcement:* tests (`test_provider_registration.py` registers a second provider from the
test alone and drives it through the factory, both middlewares and logout;
`test_keycloak_startup.py` starts the factory on each incomplete Keycloak configuration,
`test_keycloak_token_binding.py` runs the recorded Keycloak tokens through the binding,
`test_keycloak_first_login.py` checks that the organization claim has one reader,
`test_zitadel_startup.py` on each incomplete Zitadel one, `test_zitadel_token_binding.py`
runs the recorded Zitadel tokens through the binding and `test_zitadel_first_login.py`
checks that the organization claim has one reader), plus review.
