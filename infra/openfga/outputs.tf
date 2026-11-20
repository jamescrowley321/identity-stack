output "store_id" {
  description = "Id of the OpenFGA store."
  value       = openfga_store.this.id
}

output "authorization_model_id" {
  description = "Id of the authorization model every backend call names."
  value       = openfga_authorization_model.this.id
}

output "backend_environment" {
  description = "Every backend setting for this store (docs/environment-variables.md): RELATIONSHIP_STORE and the OPENFGA_* settings."
  value = {
    RELATIONSHIP_STORE             = "openfga"
    OPENFGA_API_URL                = var.api_url
    OPENFGA_STORE_ID               = openfga_store.this.id
    OPENFGA_AUTHORIZATION_MODEL_ID = openfga_authorization_model.this.id
  }
}
