output "issuer_url" {
  description = "Ory issuer for identity-stack-dev. Set as the `ory` provider row issuer_url and the frontend VITE_OIDC_AUTHORITY."
  value       = module.identity_stack_dev.issuer_url
}

output "discovery_url" {
  description = "OIDC discovery URL for identity-stack-dev."
  value       = module.identity_stack_dev.discovery_url
}

output "project_id" {
  description = "Ory project UUID for identity-stack-dev."
  value       = module.identity_stack_dev.project_id
}

output "project_slug" {
  description = "Ory project slug for identity-stack-dev."
  value       = module.identity_stack_dev.project_slug
}

output "spa_client_id" {
  description = "Public SPA client_id. Set as the frontend VITE_OIDC_CLIENT_ID."
  value       = module.identity_stack_dev.spa_client_id
}

output "project_api_key" {
  description = "Terraform-managed project API key (sensitive). Route to Infisical/HCP for the backend OrySyncAdapter; never commit."
  value       = module.identity_stack_dev.project_api_key
  sensitive   = true
}
