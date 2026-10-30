# Architecture decision records

Each ADR records one decision behind the [backend conventions](../conventions.md): what
was decided, why, and what it costs. ADRs are numbered in the order they were written and
never renumbered. An ADR that is replaced stays in this directory. Its status changes to
`Superseded by ADR-NNNN`, and the successor names it under **Supersedes**.

The first seven ADRs were written together, on 2026-10-28, for #512. ADR-0001 to ADR-0004
record decisions the code already followed. ADR-0002 records how configuration was read
before #512, and ADR-0005 replaces it.

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [0001](0001-services-return-result.md) | Services return `Result`; routers translate with `result_to_response` | Accepted | 2026-10-28 |
| [0002](0002-read-configuration-at-the-call-site.md) | Read configuration at the call site | Superseded by [0005](0005-central-settings-module.md) | 2026-10-28 |
| [0003](0003-authorization-as-route-dependencies.md) | Authorization is declared as route dependencies | Accepted | 2026-10-28 |
| [0004](0004-guards-raise-identity-errors.md) | Routing-layer guards raise `IdentityErrorRaised` | Accepted | 2026-10-28 |
| [0005](0005-central-settings-module.md) | One settings module, `app.knobs`, read at call time | Accepted | 2026-10-28 |
| [0006](0006-outbound-http-and-logging-helpers.md) | One HTTP client helper and one logger helper | Accepted | 2026-10-28 |
| [0007](0007-a-span-per-service-method.md) | Every public service method opens a span | Accepted | 2026-10-28 |

New ADRs use the same headings: **Status**, **Date**, **Context**, **Decision**,
**Consequences**, and **Supersedes** or **Superseded by** where they apply.
