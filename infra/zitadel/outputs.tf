locals {
  issuer_url = var.zitadel_url

  # The provider marks every client id sensitive, though a client id is public:
  # the SPA sends it in every authorization request.
  spa_client_id   = nonsensitive(zitadel_application_oidc.spa.client_id)
  other_client_id = nonsensitive(zitadel_application_oidc.other.client_id)
}

output "issuer_url" {
  description = "The instance's issuer, the `iss` of every token it signs."
  value       = local.issuer_url
}

output "project_id" {
  description = "Id of the identity-stack project. A human's access token lists it in `aud`."
  value       = zitadel_project.this.id
}

output "spa_client_id" {
  description = "Client id of the SPA application."
  value       = local.spa_client_id
}

output "other_client_id" {
  description = "Client id of the second application in the project, for negative tests."
  value       = local.other_client_id
}

output "organization_ids" {
  description = "Id of each organization: `platform` owns the project; the others are the fixture's tenants (infra/fixtures/test-identities.json), keyed by tenant."
  value = merge(
    { platform = zitadel_org.platform.id },
    { for key, org in zitadel_org.this : key => org.id },
  )
}

output "test_user_ids" {
  description = "Id (token `sub`) of each fixture user, keyed by user."
  value       = { for key, user in zitadel_human_user.this : key => user.id }
}

output "test_user_login_names" {
  description = "Login name of each fixture user, keyed by user."
  value       = { for key, user in zitadel_human_user.this : key => user.preferred_login_name }
}

output "test_user_passwords" {
  description = "Login password of each fixture user, keyed by user, generated at apply."
  value       = { for key, password in random_password.user : key => password.result }
  sensitive   = true
}

output "machine_user" {
  description = "The machine user for tests: its id (token `sub`) and client id (its user name)."
  value = {
    id        = zitadel_machine_user.tests.id
    client_id = nonsensitive(zitadel_machine_user.tests.client_id)
  }
}

output "machine_user_client_secret" {
  description = "Client secret of the machine user for tests (client credentials grant)."
  value       = zitadel_machine_user.tests.client_secret
  sensitive   = true
}

output "sync_admin_token" {
  description = "Personal access token of the sync adapter's machine user (identity-stack-sync, IAM_ORG_MANAGER): the backend's ZITADEL_ADMIN_TOKEN."
  value       = zitadel_personal_access_token.sync.token
  sensitive   = true
}

output "frontend_environment" {
  description = "Frontend VITE_OIDC_* settings for the SPA application."
  value = {
    VITE_OIDC_AUTHORITY    = local.issuer_url
    VITE_OIDC_CLIENT_ID    = local.spa_client_id
    VITE_OIDC_REDIRECT_URI = var.frontend_url
    # offline_access for a refresh token; the resourceowner scope puts the
    # user's organization in the access token, which decides their tenant.
    VITE_OIDC_SCOPE = "openid profile email offline_access urn:zitadel:iam:user:resourceowner"
  }
}

output "backend_environment" {
  description = "Backend ZITADEL_* settings for this instance. The audience is the project id: a human's access token lists every application of the project in `aud`, so the SPA is told apart by `client_id`."
  value = {
    ZITADEL_ISSUER_URL    = local.issuer_url
    ZITADEL_AUDIENCE      = zitadel_project.this.id
    ZITADEL_CLIENT_ID     = local.spa_client_id
    ZITADEL_REQUIRE_HTTPS = startswith(var.zitadel_url, "https://") ? "true" : "false"
  }
}

output "canonical_seed" {
  description = "What `make zitadel-seed` reads: the issuer, the tenant organizations' ids, and the fixture's two-tenant user it links with their canonical roles."
  value = {
    issuer_url = local.issuer_url
    tenants    = { for key in keys(local.tenant_organizations) : key => zitadel_org.this[key].id }
    linked_user = {
      external_sub = zitadel_human_user.this[local.linked_user].id
      email        = zitadel_human_user.this[local.linked_user].email
      user_name    = zitadel_human_user.this[local.linked_user].user_name
      given_name   = zitadel_human_user.this[local.linked_user].first_name
      family_name  = zitadel_human_user.this[local.linked_user].last_name
      roles        = local.linked_roles
    }
  }
}
