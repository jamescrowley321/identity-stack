# identity-stack Ory provisioning (workspace: auth-stack).
#
# This is the identity-stack instance of the reusable ./modules/ory-oidc-app
# pattern. Additional auth-repo projects (py-identity-model, identity-model) and
# other domains (e.g. ~/repos/gis) instantiate the same module with their own
# inputs and their own TF Cloud/HCP workspace — see README.md ("Convergence").
#
# Provider auth comes from the environment (never committed):
#   ORY_WORKSPACE_API_KEY = ory_wak_...   (workspace "auth-stack")
#   ORY_WORKSPACE_ID      = 1a710b61-9aaa-473c-aab5-77b5a5f645ad

module "identity_stack_dev" {
  source = "./modules/ory-oidc-app"

  project_name = "identity-stack-dev"
  environment  = "dev"

  spa_client_name               = "identity-stack SPA (dev)"
  spa_redirect_uris             = var.spa_redirect_uris
  spa_post_logout_redirect_uris = var.spa_post_logout_redirect_uris
  spa_audience                  = var.spa_audience
  spa_allowed_cors_origins      = var.spa_allowed_cors_origins

  identity_schema_id = "identity_stack_user_v1"

  # Develop tier: Organizations not available -> tenancy stays canonical-side.
  enable_organizations = false
}
