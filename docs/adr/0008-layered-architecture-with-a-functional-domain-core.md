# ADR-0008: Layered architecture with a functional domain core

- **Status:** Accepted
- **Date:** 2026-11-15
- **Rules:** [conventions §22–§27](../conventions.md#layers-and-the-domain)

## Context

The backend was halfway to a layered design. Services returned `Result[T, IdentityError]`
(ADR-0001), errors were frozen dataclasses, and the IdP adapters sat behind an abstract
interface. There was no domain layer, though. The classes in `app/models/identity/` are
SQLModel table rows, so the domain model and the persistence model were the same object.
Business decisions such as name uniqueness and assignment rules were made inside async
service methods that also did I/O, logging and tracing, so no rule could be tested without
a mocked repository and an event loop. Three routers called the database or Descope with no
service at all.

## Decision

The backend is domain-driven, with a functional core and an imperative shell.

### Layer map

Imports point inward: a layer imports the layers below it in this table and never one
above it.

| Layer | Package | Style | May import |
|---|---|---|---|
| Interface | `app/routers/` | Thin: parse the request, call one application service, answer with `result_to_response` | Application, and the request plumbing in `app/dependencies/`, `app/errors/` and `app/middleware/` |
| Composition | `app/dependencies/`, `app/main.py` | Builds repositories and adapters and hands them to services; holds the authorization guards | Every layer below |
| Application | `app/services/` | The imperative shell: load, decide by calling the domain, persist, invalidate, sync. Owns transactions, spans and logging | Domain, repositories, the adapter interface, `app/errors/` |
| Infrastructure | `app/repositories/`, `app/models/`, `app/services/adapters/` | Repositories run SQL and map table rows to and from domain values. Adapters are the anti-corruption layer to Descope and Ory | Domain and the frameworks |
| Domain | `app/domain/<context>/` | Pure: frozen dataclasses, smart constructors and decision functions that return `Result`, domain events as values | The standard library and `expression` |

`app/knobs.py`, `app/logbook.py`, `app/outbound.py` and `app/telemetry.py` are helpers any
layer above the domain may use.

### The domain's shape

Each bounded context is one package, `app/domain/<context>/`, and other layers import the
package, not its modules. A context holds:

- **Values:** entities and value objects as `@dataclass(frozen=True)`. A value object with
  an invariant has a smart constructor, such as `role_name(raw) -> Result[RoleName, InvalidName]`.
  A repository rebuilding a stored value calls the type directly, because a stored value is
  trusted, not re-validated.
- **Commands:** what a caller asks for, as frozen dataclasses.
- **Decisions:** functions `(state, command, *, ids, now) -> Result[(new value, event), error]`.
  The caller loads the state. Absent state is `None`, so "not found" is the decision's answer.
- **Events:** what a decision says has happened, such as `RoleCreated`. Decisions return
  them from the start. Until #518 the service uses an event to pick the cache invalidation
  it publishes, and still calls the IdP adapter inline.
- **Errors:** frozen dataclasses under one base per context (`AccessError`).
  `app/errors/access.py` translates each one to the `IdentityError` a service returns.

A service method is then a fixed sequence: build the command, load, decide, persist,
commit, invalidate, sync. What stays in the service is what only the database can
answer: a uniqueness race caught as `RepositoryConflictError`, and a delete that removed
no row.

### Context map

| Context | Covers | Domain package | State |
|---|---|---|---|
| Access | Roles, permissions, assignments | `app/domain/access/` | Migrated (#513) |
| Directory | Users, tenants, memberships | `app/domain/directory/` | #515 |
| Federation | Providers, IdP links, identity resolution, just-in-time provisioning, logout | `app/domain/federation/` | #516 |
| Documents and fine-grained authorization | Documents and their FGA relations | `app/domain/documents/` | #517 |
| Synchronisation | Domain events, the Descope and Ory adapters, inbound sync, reconciliation, sync status, cache invalidation | `app/domain/synchronisation/` | #518 |
| Descope management boundary | The pass-through routers, moved behind one adapter | none: a boundary, not a domain | #519 |

Relationships:

- **Directory is upstream of Access, Federation and Documents.** They hold a user or a
  tenant by id and never import Directory's types.
- **Synchronisation is downstream of every domain context.** It consumes their events and
  owns the anti-corruption layer to the providers.
- **The Descope management boundary** is independent of the domain contexts.

The package names of the unmigrated contexts are proposals. Each context's issue settles
its own.

### Glossary: Access

| Term | Meaning |
|---|---|
| Role | A named set of permissions. Its name is unique within its scope. |
| Scope | Where a role's name must be unique: one tenant, or global when the role has no tenant. |
| Permission | A named capability, such as `documents.write`. Its name is unique across the system. |
| Mapping | A permission attached to a role. "Map" is the verb the API already uses. |
| Assignment | A role a user holds in one tenant, with who assigned it and when. |
| Name holder | The role or permission that already has a name a command asks for. |

### Glossary: Directory

| Term | Meaning |
|---|---|
| User | A canonical user: the application's own record of a person, independent of any provider. |
| Tenant | An organisation or workspace that users belong to and that scopes roles and documents. |
| Membership | A user's belonging to a tenant. |

### Glossary: Federation

| Term | Meaning |
|---|---|
| Provider | A registered identity provider, such as Descope or Ory. |
| IdP link | The tie between a canonical user and that user's identity at one provider. |
| Identity resolution | Finding the canonical user, tenant, roles and permissions for a provider's subject. |
| Just-in-time provisioning | Creating the canonical user and its links on first sight of a valid token. |
| Logout strategy | How a session is ended at a given provider. |

### Glossary: Documents and fine-grained authorization

| Term | Meaning |
|---|---|
| Document | A tenant-scoped resource whose access is decided per user. |
| Relation | A fine-grained authorization fact: this user has this relation to this document. |

### Glossary: Synchronisation

| Term | Meaning |
|---|---|
| Outbound sync | Pushing a canonical change to a provider. |
| Inbound sync | Applying a provider's flow or webhook event to the canonical store. |
| Sync event | The append-only record of one inbound event. |
| Reconciliation | Comparing the canonical store with a provider and repairing the drift. |
| Cache invalidation | Telling other instances that a cached identity is stale. |

### Glossary: Descope management boundary

| Term | Meaning |
|---|---|
| Pass-through router | A router that forwards a request to the Descope Management API. |
| Management client | `DescopeManagementClient`, the one caller of that API (conventions §5). |

### Enforcement

`import-linter` runs in `make lint` and in CI with four contracts: the layer order, the
domain's forbidden imports, independence between contexts, and a `context_layers` contract
(`backend/import_contracts.py`) for the rules that forbid skipping a layer. That last
contract takes one `unmigrated` entry per context, naming the modules that still break a
rule. A migration deletes its entry, and the contract fails on an entry that no longer
excuses anything. `tests/unit/test_house_rules.py` checks what an import graph cannot see:
that the domain reads no clock and makes no ids, raises nothing and has only frozen types.

## Consequences

- A business rule is a pure function. Its tests need no database, no event loop and no
  mocks (`backend/tests/unit/domain/`).
- The HTTP API is unchanged. Messages and payloads are pinned by
  `tests/unit/test_access_service_contract.py`.
- The repository classes still take and return table rows, because unmigrated services
  share them. For a migrated context the mapping lives beside the repository as functions
  (`role_from_row`, `role_to_row`, `apply_role`), and the service holds a row only long
  enough to pass it back. Once every caller of a repository is migrated, its methods can
  take and return domain values directly.
- The service supplies ids and the time, so the table models' own defaults no longer fire
  for a migrated context. `updated_at` is the exception: the database still stamps it on
  write, and the service returns the value it re-reads.
- A decision cannot see a concurrent writer. The unique constraints remain the final
  check, and the service turns a violation into the same error the decision would give.
- `context_layers` is a custom contract type, because the built-in contracts take
  exceptions per import, not per context. It is one small module with its own tests.
- Contexts may not import each other at all. A shared kernel, if one is needed, gets its
  own ADR.
- Routers keep some request-shaped rules for now, such as "only an owner may assign the
  owner role" in `routers/roles.py`. Moving them is left to a follow-up, because it changes
  where a 403 is produced.
