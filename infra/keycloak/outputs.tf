locals {
  issuer_url = "${var.keycloak_url}/realms/${keycloak_realm.this.realm}"
}

output "backend_environment" {
  description = "Backend KEYCLOAK_* settings for this realm (docs/environment-variables.md), except the directory client secret: see `keycloak_directory_client_secret`."
  value = {
    KEYCLOAK_ISSUER_URL               = local.issuer_url
    KEYCLOAK_AUDIENCES                = join(",", sort(keys(local.type_clients)))
    KEYCLOAK_APP_CLIENTS              = join(",", [for type_client in sort(keys(local.type_clients)) : "${type_client}=${keycloak_openid_client.app[type_client].client_id}"])
    KEYCLOAK_PORTABLE_TYPES           = join(",", sort([for type_client, spec in local.type_clients : type_client if spec.portable]))
    KEYCLOAK_ORGANIZATION_BOUND_TYPES = join(",", sort([for type_client, spec in local.type_clients : type_client if !spec.portable]))
    KEYCLOAK_PORTABLE_PERMISSIONS = join(",", sort(flatten([
      for type_client, spec in local.type_clients : [for permission in spec.portable_permissions : "${type_client}=${permission}"]
    ])))
    KEYCLOAK_REQUIRE_HTTPS            = startswith(var.keycloak_url, "https://") ? "true" : "false"
    KEYCLOAK_POST_LOGOUT_REDIRECT_URI = "${var.frontend_url}/login"
    KEYCLOAK_DIRECTORY_CLIENT_ID      = keycloak_openid_client.directory.client_id
  }
}

output "keycloak_directory_client_secret" {
  description = "Backend KEYCLOAK_DIRECTORY_CLIENT_SECRET: the secret Keycloak generated for the directory client."
  value       = keycloak_openid_client.directory.client_secret
  sensitive   = true
}

output "frontend_environment" {
  description = "Frontend VITE_OIDC_* settings, one map per app client (the SPA logs in through one at a time)."
  value = {
    for type_client in sort(keys(local.type_clients)) : keycloak_openid_client.app[type_client].client_id => {
      VITE_OIDC_AUTHORITY    = local.issuer_url
      VITE_OIDC_CLIENT_ID    = keycloak_openid_client.app[type_client].client_id
      VITE_OIDC_REDIRECT_URI = var.frontend_url
    }
  }
}

output "user_ids" {
  description = "Keycloak-generated id (token sub) of each fixture user."
  value       = { for username, user in keycloak_user.this : username => user.id }
}

output "user_passwords" {
  description = "Login password of each fixture user, generated at apply."
  value       = { for username, password in random_password.user : username => password.result }
  sensitive   = true
}
