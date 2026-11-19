# OpenFGA store in Terraform (`infra/openfga`)

The relationship store of a deployment whose provider has no Zanzibar-style store of its own
(`RELATIONSHIP_STORE=openfga`, the default for `SYNC_PROVIDER=keycloak` and `zitadel`; an Ory
deployment sets it itself). The backend's FGA and document routes check and write relations here,
keyed on canonical tenant and user ids ([conventions §18](../../docs/conventions.md)).

It uses the `openfga/openfga` provider (`~> 0.5.1`). It targets the local compose servers: the
`openfga` profile of `docker-compose.yml` (`http://localhost:8380`, the default `api_url`) or
`openfga-test` in `docker-compose.test.yml` (`http://localhost:18380`). State is local
(`terraform.tfstate` in this directory) and git-ignored; `.terraform.lock.hcl` is committed.

## What it creates

| Resource | What |
|---|---|
| `openfga_store.this` | The store (`var.store_name`, default `identity-stack`). |
| `openfga_authorization_model.this` | The authorization model, [`model.json`](model.json): types `user` and `document`; `owner`, `editor` and `viewer` taken by a user; `can_view`, `can_edit` and `can_delete` derived from them. The same relations as the Descope schema in [`../fga.tf`](../fga.tf). |

Outputs: `store_id`, `authorization_model_id`, and `backend_environment`, the backend's
`RELATIONSHIP_STORE`, `OPENFGA_API_URL`, `OPENFGA_STORE_ID` and `OPENFGA_AUTHORIZATION_MODEL_ID`
([environment variables](../../docs/environment-variables.md)).

`OPENFGA_API_URL` in the output is `api_url`, the address Terraform reached OpenFGA at: right for a
backend on the host (`make dev-backend`, CI). A backend in the compose network (`docker compose up`
with the `openfga` profile) reaches the same server as `http://openfga:8080`: set that instead
in `.env`; the store and model ids stay the same.

The model lives here only. OpenFGA never changes a model: a changed `model.json` writes a new
model with a new id, and the backend uses the id it was started with, so restart it with the new
output. The backend never writes a model: `PUT /api/fga/schema` answers 409 on OpenFGA. Tuples the
Descope store holds are not copied here.

## Run it

```bash
docker compose --profile openfga up -d --wait openfga
terraform -chdir=infra/openfga init
terraform -chdir=infra/openfga apply
terraform -chdir=infra/openfga output backend_environment   # into backend/.env
```

`make openfga-terraform-check` applies a copy of the root to `openfga-test` (`make test-up-openfga`
first), checks that a second plan is empty and that the server holds `model.json`'s types and
relations under the applied id, then destroys its store. CI's `OpenFGA Terraform Check` job runs
it. The backend's integration suite writes the same `model.json` to a store of its own and runs
the relationship-store contract against it
([`backend/tests/integration/test_openfga_relationship_store.py`](../../backend/tests/integration/test_openfga_relationship_store.py)).
