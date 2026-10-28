# ADR-0007: Every public service method opens a span

- **Status:** Accepted
- **Date:** 2026-10-28
- **Rules:** [conventions §20](../conventions.md#20-every-public-service-method-opens-its-own-span)

## Context

The canonical services mostly followed one pattern: a module-level
`tracer = trace.get_tracer(__name__)` and a `<Service>.<method>` span around each public
method. Coverage was uneven. `UserService`, `RoleService`, `PermissionService`,
`ProviderService` and the rest spanned every public method, but
`SyncStatusService.record_event` did not. `DescopeManagementClient`,
`CacheInvalidationPublisher` and the logout strategies opened no spans at all. A trace for
a request that called Descope therefore showed only the bare httpx client span from the
OpenTelemetry httpx instrumentor, with nothing naming the operation.

## Decision

- Every public method of a class in `app/services/` opens a span named `<Class>.<method>`
  around its body. A public module-level function opens `<module>.<function>`, and each
  logout strategy opens `logout.<logout_kind>`.
- Abstract methods, lifecycle `close`, pure helpers and the lifetime-long cache
  subscriber are named exceptions in §20. The provider sync adapters keep their existing
  `<provider>.<operation>` names.
- `tests/unit/test_house_rules.py` checks the rule from the source, so a new method
  without its span fails `make test-unit`.

## Consequences

- This is the one intended runtime change in #512. Requests that reach these code paths
  emit additional spans. Responses, status codes and the API are unchanged, and nothing
  is emitted unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set.
- Wrapping a body in `with tracer.start_as_current_span(...)` records an exception that
  escapes the method on its span and re-raises it unchanged. Callers see the same
  exceptions as before.
- `DescopeManagementClient` spans are the exception to OpenTelemetry's default error
  handling. Many callers expect and tolerate an upstream 4xx, so these spans record neither
  the exception nor an ERROR status. Otherwise a successful revoke, or a lookup answered with
  defaults, would show up in traces as a failure. The httpx client span beneath still
  records the HTTP status.
- A method with nothing to do (`CacheInvalidationPublisher.publish` without Redis) returns
  before opening its span, so single-instance deployments do not emit an empty span per
  mutation.
