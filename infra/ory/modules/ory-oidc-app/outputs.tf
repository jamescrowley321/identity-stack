output "project_id" {
  description = "Ory project UUID."
  value       = ory_project.this.id
}

output "project_slug" {
  description = "Ory project slug (used in API/issuer URLs)."
  value       = ory_project.this.slug
}

output "project_state" {
  description = "Ory project state."
  value       = ory_project.this.state
}

output "issuer_url" {
  description = "OAuth2/OIDC issuer for this project. Feeds the canonical `providers` registry row and the backend issuer allow-list."
  value       = "https://${ory_project.this.slug}.projects.oryapis.com"
}

output "discovery_url" {
  description = "OIDC discovery document URL (mirrors py-identity-model's TEST_DISCO_ADDRESS shape)."
  value       = "https://${ory_project.this.slug}.projects.oryapis.com/.well-known/openid-configuration"
}

output "spa_client_id" {
  description = "Public SPA OAuth2 client_id. Feeds the frontend VITE_OIDC_CLIENT_ID."
  value       = ory_oauth2_client.spa.client_id
}

output "project_api_key" {
  description = "Terraform-managed project API key value (sensitive). For the backend OrySyncAdapter admin operations — route to Infisical/HCP, never commit."
  value       = ory_project_api_key.tf.value
  sensitive   = true
}
