output "issuer_url" {
  description = "The realm's issuer, the `iss` of every token it signs."
  value       = local.issuer_url
}

output "api_audience" {
  description = "Client id of the API client: the SPA's access tokens list it in `aud`."
  value       = keycloak_openid_client.api.client_id
}

output "spa_client_id" {
  description = "Client id of the SPA's public client: the `azp` of its tokens."
  value       = keycloak_openid_client.public[local.spa_client_id].client_id
}

output "other_client_id" {
  description = "Client id of the other public client, whose tokens lack the API audience, for negative tests."
  value       = keycloak_openid_client.public[local.other_client_id].client_id
}

output "organization_aliases" {
  description = "Alias of each organization (the token's `organization` claim), keyed by the fixture's tenant (infra/fixtures/test-identities.json)."
  value       = { for key, organization in keycloak_organization.this : key => organization.alias }
}

output "user_ids" {
  description = "Keycloak-generated id (token sub) of each fixture user."
  value       = { for username, user in keycloak_user.this : username => user.id }
}

output "backend_environment" {
  description = "Every backend KEYCLOAK_* setting for this realm (docs/environment-variables.md), the admin service account's secret included. `make keycloak-env` prints it in .env form."
  sensitive   = true
  value = {
    KEYCLOAK_ISSUER_URL               = local.issuer_url
    KEYCLOAK_AUDIENCE                 = keycloak_openid_client.api.client_id
    KEYCLOAK_CLIENT_ID                = keycloak_openid_client.public[local.spa_client_id].client_id
    KEYCLOAK_REQUIRE_HTTPS            = module.urls.require_https
    KEYCLOAK_POST_LOGOUT_REDIRECT_URI = "${local.frontend_url}/login"
    KEYCLOAK_ADMIN_CLIENT_ID          = keycloak_openid_client.admin.client_id
    KEYCLOAK_ADMIN_CLIENT_SECRET      = keycloak_openid_client.admin.client_secret
  }
}

output "frontend_environment" {
  description = "Frontend VITE_OIDC_* settings for the SPA's client. `make keycloak-env` prints it in .env form."
  value = {
    VITE_OIDC_AUTHORITY    = local.issuer_url
    VITE_OIDC_CLIENT_ID    = keycloak_openid_client.public[local.spa_client_id].client_id
    VITE_OIDC_REDIRECT_URI = local.frontend_url
    # `organization` is an optional scope on the SPA client: asked bare,
    # Keycloak puts the user's organization in the token, and asks a member of
    # several which one.
    VITE_OIDC_SCOPE = "openid profile email organization"
  }
}

output "test_credentials" {
  description = "What tests log in with: each fixture user's password (generated at apply), keyed by user. No client here is for tests only, so `clients` is empty: the admin service account's secret is a backend setting (backend_environment)."
  sensitive   = true
  value = {
    users   = { for username, password in random_password.user : username => password.result }
    clients = {}
  }
}
