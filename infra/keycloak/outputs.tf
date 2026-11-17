locals {
  # The type client whose app client the SPA logs in through: the portable
  # member type, as in the compose e2e build. The other app clients take the
  # same settings with their own VITE_OIDC_CLIENT_ID.
  spa_type_client = "member"
}

output "backend_environment" {
  description = "Every backend KEYCLOAK_* setting for this realm (docs/environment-variables.md), the service accounts' secrets included. `make keycloak-env` prints it in .env form."
  sensitive   = true
  value = {
    KEYCLOAK_ISSUER_URL               = local.issuer_url
    KEYCLOAK_AUDIENCES                = join(",", sort(keys(local.type_clients)))
    KEYCLOAK_APP_CLIENTS              = join(",", [for type_client in sort(keys(local.type_clients)) : "${type_client}=${keycloak_openid_client.app[type_client].client_id}"])
    KEYCLOAK_PORTABLE_TYPES           = join(",", sort([for type_client, spec in local.type_clients : type_client if spec.portable]))
    KEYCLOAK_ORGANIZATION_BOUND_TYPES = join(",", sort([for type_client, spec in local.type_clients : type_client if !spec.portable]))
    KEYCLOAK_PORTABLE_PERMISSIONS = join(",", sort(flatten([
      for type_client, spec in local.type_clients : [for permission in spec.portable_permissions : "${type_client}=${permission}"]
    ])))
    KEYCLOAK_REQUIRE_HTTPS            = module.urls.require_https
    KEYCLOAK_POST_LOGOUT_REDIRECT_URI = "${local.frontend_url}/login"
    KEYCLOAK_DIRECTORY_CLIENT_ID      = keycloak_openid_client.directory.client_id
    KEYCLOAK_DIRECTORY_CLIENT_SECRET  = keycloak_openid_client.directory.client_secret
    KEYCLOAK_ADMIN_CLIENT_ID          = keycloak_openid_client.admin.client_id
    KEYCLOAK_ADMIN_CLIENT_SECRET      = keycloak_openid_client.admin.client_secret
  }
}

output "frontend_environment" {
  description = "Frontend VITE_OIDC_* settings for the SPA's app client (the member type's). `make keycloak-env` prints it in .env form."
  value = {
    VITE_OIDC_AUTHORITY    = local.issuer_url
    VITE_OIDC_CLIENT_ID    = keycloak_openid_client.app[local.spa_type_client].client_id
    VITE_OIDC_REDIRECT_URI = local.frontend_url
    # `organization` is an optional scope on the app clients: without it the
    # token has no organization claim and an organization-bound type is
    # refused. Asked bare, Keycloak puts the user's organization in the
    # token, and asks a member of several which one.
    VITE_OIDC_SCOPE = "openid profile email organization"
  }
}

output "test_credentials" {
  description = "What tests log in with: each fixture user's password (generated at apply), keyed by user. No client here is for tests only, so `clients` is empty: the service accounts' secrets are backend settings (backend_environment)."
  sensitive   = true
  value = {
    users   = { for username, password in random_password.user : username => password.result }
    clients = {}
  }
}

output "user_ids" {
  description = "Keycloak-generated id (token sub) of each fixture user."
  value       = { for username, user in keycloak_user.this : username => user.id }
}
