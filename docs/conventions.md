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

Enforcement is noted under each rule. **Ruff** means `make lint` fails, and **test**
means `make test-unit` fails. Rules marked **review** are enforced by a reviewer.

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
`UpstreamTransportError` and answer them with 502, as `documents.py`, `fga.py` and
`attributes.py::get_profile` do. Otherwise a Descope outage surfaces as an unhandled 500. A
function that catches `Exception` counts as handling both.

- Exception: the five handlers in `routers/users.py`, plus `update_profile_attribute`,
  `get_tenant_settings` and `update_tenant_settings` in `routers/attributes.py`. They catch
  only `UpstreamStatusError`, so a transport failure surfaces as 500. That predates #512, and
  changing those answers to 502 is a behaviour change left for a follow-up.
- Exception: every function in `routers/accesskeys.py` (its six handlers and
  `_verify_key_tenant`). None of them catches an upstream failure, so every Descope error and
  timeout there surfaces as 500. That also predates #512 and is left for the same follow-up.
- Exception: `routers/users.py::_get_descope_client`, which only returns the client and makes
  no call.

*Enforcement:* Ruff (TID251 on `httpx`), plus a test
(`test_router_functions_that_call_descope_handle_upstream_failures`). The test checks each
router function that reads `descope_client` or calls a same-module helper that does, and keys
its exceptions by module and function.

### 5. The Descope Management API is called through `DescopeManagementClient`

Calls to Descope's `/v1/mgmt/...` endpoints **MUST** go through
`app.services.descope.DescopeManagementClient`. Routers and dependencies use the
process-wide instance on `request.app.state.descope_client`. Code **MUST NOT** build a
management URL or an `Authorization: Bearer <project>:<key>` header itself.

- Exception: `app/services/logout.py` (`_descope_management_logout`). It calls
  `/v1/mgmt/user/logout` with its own short-lived client, and that request is preserved
  byte-for-byte (NFR-8).

*Enforcement:* review.

### 6. Every outbound call has an explicit timeout

Every outbound HTTP call **MUST** carry a timeout chosen by its caller. `outbound_client`
makes `timeout` a required keyword-only argument, so a call site cannot fall back to an
implicit default.

- Exception: OIDC discovery and JWKS fetches made inside `py-identity-model`. The library
  builds its own clients with a timeout taken from its `HTTP_TIMEOUT` environment variable
  (default 30 seconds). The backend does not read that variable.

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
`INTERNAL_IDENTITY_KEY` and raw `Authorization` headers. Log identifiers instead, such as a
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
  endpoint's published contract.

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

A route is tenant-scoped when it depends on `get_tenant_id` or `get_tenant_claims`. Every
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
`get_claims`, `get_current_user`, `get_tenant_id` and `get_tenant_claims`. In particular, a
handler **MUST NOT** read the `dct` claim to find its tenant. It takes the tenant from
`get_tenant_id`.

- Exception: `routers/auth.py::logout`, which needs the principal or `None` so it can fall
  back to the first configured provider.
- Exception: `routers/protected.py::canonical_identity`, which checks principal, identity
  and claims together for one 401.
- Exception: `routers/idp_links.py::_require_user_in_tenant`, a raising guard that answers
  404 rather than 401/403 so it does not reveal whether a user exists.

*Enforcement:* test (`test_router_code_does_not_read_request_state`).

### 17. Authorization fails closed

An authorization dependency **MUST** refuse (401 or 403) on every branch where it cannot
reach an unambiguous decision. That includes a missing subject, missing claims, no tenant
or several candidate tenants, a non-active canonical user and a malformed `dct`. An error
from the FGA store **MUST** be answered with 502 and treated as a denial, never as an allow.

*Enforcement:* review, plus tests (`test_rbac_dependencies.py`, `test_fga_dependencies.py`).

### 18. Descope principals are authorized from Descope's own claims

`require_role` and `require_permission` **MUST** decide a Descope principal from its signed
`dct` and `tenants` claims alone. Roles and permissions **MUST** be resolved from the
canonical model only for a provider listed in `CANONICAL_RBAC_PROVIDERS` (default `ory`).
That canonical lookup **MUST NOT** be served from the identity cache.

*Enforcement:* review, plus tests (`test_rbac_dependencies.py`).

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
