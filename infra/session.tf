# Session management settings for the shared Descope project.
#
# These settings must be configured in the Descope console (or via API)
# until a `data "descope_project"` data source is available in the provider.
# See: https://github.com/jamescrowley321/terraform-provider-descope/issues/52
#
# Required settings:
#   - Refresh token rotation:    enabled
#   - Session token expiration:  10 minutes
#   - Refresh token expiration:  4 weeks
#   - Inactivity timeout:        enabled, 30 minutes

locals {
  session_settings = {
    refresh_token_rotation  = true
    session_token_expiration = "10 minutes"
    refresh_token_expiration = "4 weeks"
    enable_inactivity        = true
    inactivity_time          = "30 minutes"
  }
}
