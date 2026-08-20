# Reusable Ory-Network OIDC-app module.
#
# Provisions one Ory project configured as an OIDC provider for a single-page app:
#   - JWT access-token strategy (local validation via py-identity-model)
#   - PKCE-enforced public SPA OAuth2 client (authorization_code + refresh)
#   - a SCIM-aligned default identity schema
#   - an optional B2B Organization (flag-gated; off by default)
#
# Auth model (from the ory/ory provider schema):
#   - ory_project / ory_project_config / ory_identity_schema / ory_project_api_key
#     are workspace-scoped: they use the provider's workspace_api_key + project_id.
#   - ory_oauth2_client is project-scoped: it authenticates with project_slug +
#     project_api_key. We mint that key here (ory_project_api_key) and feed it in,
#     so the whole graph applies in one pass from a single workspace API key.

resource "ory_project" "this" {
  name        = var.project_name
  environment = var.environment
  home_region = var.home_region
}

resource "ory_project_config" "this" {
  project_id = ory_project.this.id

  # Token model: JWT access tokens, validated locally by py-identity-model.
  oauth2_strategies_access_token  = var.access_token_strategy
  oauth2_allowed_top_level_claims = var.allowed_top_level_claims

  # Baseline OIDC hardening. FAPI 2.0 is intentionally NOT a target.
  oauth2_pkce_enforced_for_public_clients = var.enforce_pkce_public_clients
}

# Project API key used to (a) create the project-scoped OAuth2 client below and
# (b) later authenticate the backend OrySyncAdapter's admin operations.
# The value is sensitive and lives only in Terraform state (never committed).
resource "ory_project_api_key" "tf" {
  project_id = ory_project.this.id
  name       = var.project_api_key_name
}

# Public SPA client: authorization_code + PKCE, no secret (token_endpoint_auth_method = none).
resource "ory_oauth2_client" "spa" {
  project_slug    = ory_project.this.slug
  project_api_key = ory_project_api_key.tf.value

  client_name                = var.spa_client_name
  grant_types                = var.spa_grant_types
  response_types             = ["code"]
  token_endpoint_auth_method = "none"
  redirect_uris              = var.spa_redirect_uris
  post_logout_redirect_uris  = var.spa_post_logout_redirect_uris
  scope                      = var.spa_scope
  access_token_strategy      = var.access_token_strategy
}

# SCIM-aligned identity schema: email (identifier, verification/recovery via email)
# plus given/family name, matching the canonical `users` model.
resource "ory_identity_schema" "user" {
  project_id  = ory_project.this.id
  schema_id   = var.identity_schema_id
  set_default = true

  schema = jsonencode({
    "$id"     = "https://schemas.ory.sh/${var.identity_schema_id}.json"
    "$schema" = "http://json-schema.org/draft-07/schema#"
    title     = var.identity_schema_title
    type      = "object"
    properties = {
      traits = {
        type = "object"
        properties = {
          email = {
            type   = "string"
            format = "email"
            title  = "Email"
            "ory.sh/kratos" = {
              credentials = {
                password = { identifier = true }
                code     = { identifier = true, via = "email" }
              }
              verification = { via = "email" }
              recovery     = { via = "email" }
            }
          }
          given_name = {
            type  = "string"
            title = "Given name"
          }
          family_name = {
            type  = "string"
            title = "Family name"
          }
        }
        required             = ["email"]
        additionalProperties = false
      }
    }
  })
}

# Optional B2B Organization ↔ canonical tenant. Off by default (canonical-side tenancy).
resource "ory_organization" "org" {
  count      = var.enable_organizations ? 1 : 0
  project_id = ory_project.this.id
  label      = var.organization_label
}
