# Shared test fixture (`infra/fixtures/`)

[`test-identities.json`](test-identities.json) is the one definition of the test tenants and
users. Every Terraform root that seeds a provider builds its tenants, users and memberships from
it, so an end-to-end scenario means the same thing on each provider. Nothing in it is a secret:
passwords and ids are each provider's own.

| Key | What |
|---|---|
| `tenants` | Each tenant's display `name`, email `domain`, and `canonical`: whether a canonical tenant names it. `descope` is the Descope tenant it maps to (`infra/tenants.tf`); `ory` is null for every tenant, because the Ory project has no organizations (`infra/ory/main.tf`). |
| `users` | Each user's `first_name`, `last_name`, `home` tenant and `roles`: each tenant the user is a member of, to the user's canonical role there (`owner`, `admin`, `member`, `viewer`), or null for a member with no grant. A user's email is `<key>@<domain of home>`, and the user is a member of their home tenant. |
| `cases` | The user each case of the suites stands on. |

## The tenants and users

| User | Home | Roles | Case |
|---|---|---|---|
| `alice` | `acme` | `acme`: `member` | `member`: a member of one tenant |
| `bob` | `globex` | `acme`: `admin`, `globex`: `viewer` | `two_tenants`: a member of two tenants, with a different role in each |
| `carol` | `acme` | `acme`: none | `no_grant`: a member with no grant |
| `dave` | `acme` | `acme`: `admin` | `tenant_admin`: an admin of exactly one tenant |
| `erin` | `acme` | `acme`: `member` | `write_only`: a member of one tenant, a second user beside `member` |
| `frank` | `acme` | `acme`: `admin` | `moving_admin`: an admin whom tests move to another tenant and back |
| `grace` | `unlinked` | `unlinked`: `member` | `unlinked_tenant`: the member of a tenant no canonical tenant names |

`acme` (Acme, `acme.test`) and `globex` (Globex, `globex.test`) are canonical tenants and map to
the Descope tenants "Acme Corp" and "Globex Inc". `unlinked` (Unlinked, `unlinked.test`) is named
by no canonical tenant and has no Descope tenant.

## Who reads it

- [`infra/keycloak`](../keycloak/README.md): an organization per tenant, a user per user, the
  members, and each user's role in each tenant as membership of that organization's group for the
  role (a record that grants nothing). The realm import
  [`keycloak/realm/identity-stack-realm.json`](../../keycloak/realm/identity-stack-realm.json)
  is a file of its own, with fixed user ids; `make keycloak-terraform-check` compares it with the
  Terraform-built realm, and `backend/tests/unit/test_identity_fixture.py` with this fixture.
- [`infra/zitadel`](../zitadel/README.md): an organization per tenant, the project granted to the
  canonical ones, and a human user per user in the organization of their home tenant (a Zitadel
  user lives in exactly one organization). The canonical store decides access, so the fixture's
  roles reach Zitadel only through `make zitadel-seed`, which links the `two_tenants` user with
  their roles in the canonical tenants; every other user is provisioned at first login.
- The backend's Keycloak and Zitadel suites take user names and emails from it
  (`backend/tests/helpers/identity_fixture.py`), or from a root's outputs.

The Descope and Ory roots are hosted and are not built from it.

A change here changes both local roots: add a user to the Keycloak realm import too, with a fixed
id and a `local-only-<user>` password, or the unit test above fails.
