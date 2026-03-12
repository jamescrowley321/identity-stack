# Session management settings for the shared Descope project.
#
# The project itself is created externally (py-identity-model/infra/descope).
# This resource imports it by ID and configures session-related settings.
#
# First-time setup:
#   terraform import descope_project.main <project_id>

resource "descope_project" "main" {
  name = "descope-saas-starter"

  project_settings {
    # Rotate refresh tokens on each use to limit replay attacks.
    refresh_token_rotation = true

    # Session (access) token lifetime — short-lived for security.
    session_token_expiration = "10 minutes"

    # Refresh token lifetime — how long a user stays logged in without re-authenticating.
    refresh_token_expiration = "4 weeks"

    # Boot idle users after a period of no activity.
    enable_inactivity = true
    inactivity_time   = "30 minutes"
  }
}
