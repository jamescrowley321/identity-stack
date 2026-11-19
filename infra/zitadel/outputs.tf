locals {
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

output "machine_user" {
  description = "The machine user for tests: its id (token `sub`) and client id (its user name)."
  value = {
    id        = zitadel_machine_user.tests.id
    client_id = nonsensitive(zitadel_machine_user.tests.client_id)
  }
}

output "frontend_environment" {
  description = "Frontend VITE_OIDC_* settings for the SPA application. `make zitadel-env` prints it in .env form."
  value = {
    VITE_OIDC_AUTHORITY    = local.issuer_url
    VITE_OIDC_CLIENT_ID    = local.spa_client_id
    VITE_OIDC_REDIRECT_URI = local.frontend_url
    # offline_access for a refresh token; the resourceowner scope puts the
    # user's organization in the access token, which decides their tenant.
    VITE_OIDC_SCOPE = "openid profile email offline_access urn:zitadel:iam:user:resourceowner"
  }
}

output "backend_environment" {
  description = "Every backend ZITADEL_* setting for this instance, the sync adapter's token included. The audience is the project id: a human's access token lists every application of the project in `aud`, so the SPA is told apart by `client_id`. `make zitadel-env` prints it in .env form."
  sensitive   = true
  value = {
    ZITADEL_ISSUER_URL    = local.issuer_url
    ZITADEL_AUDIENCE      = zitadel_project.this.id
    ZITADEL_CLIENT_ID     = local.spa_client_id
    ZITADEL_REQUIRE_HTTPS = module.urls.require_https
    # Personal access token of the sync adapter's machine user
    # (identity-stack-sync, IAM_ORG_MANAGER).
    ZITADEL_ADMIN_TOKEN = zitadel_personal_access_token.sync.token
  }
}

output "test_credentials" {
  description = "What tests log in with: each fixture user's password (generated at apply), keyed by user, and the test machine user's client secret (client credentials grant), keyed by its client id."
  sensitive   = true
  value = {
    users   = { for key, password in random_password.user : key => password.result }
    clients = { (nonsensitive(zitadel_machine_user.tests.client_id)) = zitadel_machine_user.tests.client_secret }
  }
}

output "share_target_seed" {
  description = "What `make zitadel-seed` reads second: canonical_seed with the fixture's second member of the two-tenant user's admin tenant as its linked user, so the shared e2e suite can share a document with them."
  value = {
    issuer_url = local.issuer_url
    tenants    = { for key in keys(local.tenant_organizations) : key => zitadel_org.this[key].id }
    linked_user = {
      external_sub = zitadel_human_user.this[local.share_target].id
      email        = zitadel_human_user.this[local.share_target].email
      user_name    = zitadel_human_user.this[local.share_target].user_name
      given_name   = zitadel_human_user.this[local.share_target].first_name
      family_name  = zitadel_human_user.this[local.share_target].last_name
      roles        = local.share_target_roles
    }
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
