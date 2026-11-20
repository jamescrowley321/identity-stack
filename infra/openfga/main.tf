# The backend's relationship store on OpenFGA (RELATIONSHIP_STORE=openfga): one
# store and the authorization model of the FGA and document routes. The model
# is model.json, the same document the integration tests write to a fresh store
# (backend/tests/integration/test_openfga_relationship_store.py). It mirrors the
# Descope schema in ../fga.tf: owner, editor and viewer, and can_view, can_edit
# and can_delete derived from them.
#
# OpenFGA never changes a model: a changed model.json writes a new model with a
# new id, which the backend reads from OPENFGA_AUTHORIZATION_MODEL_ID at startup.

resource "openfga_store" "this" {
  name = var.store_name
}

# Normalizes the JSON, so a change of formatting in model.json plans nothing.
data "openfga_authorization_model_document" "this" {
  json = file("${path.module}/model.json")
}

resource "openfga_authorization_model" "this" {
  store_id   = openfga_store.this.id
  model_json = data.openfga_authorization_model_document.this.result
}
