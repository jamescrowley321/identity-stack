variable "e2e_test_email" {
  description = <<-EOT
    Email address for the E2E test user, which must hold the admin role in the
    Acme tenant. Written to the E2E_TEST_EMAIL GitHub Actions secret.

    No default, for the same reason as descope_management_key: a blank value
    here silently disables authenticated E2E coverage rather than failing.
  EOT
  type        = string

  validation {
    condition     = length(trimspace(var.e2e_test_email)) > 0
    error_message = "e2e_test_email must be non-empty; refusing to overwrite the CI secret with a blank value."
  }
}

# GitHub Actions
variable "github_repository" {
  description = "GitHub repository name (without owner) for CI secrets"
  type        = string
  default     = "identity-stack"
}

# Session settings
variable "descope_project_name" {
  description = "Display name for the Descope project"
  type        = string
  default     = "identity-stack"
}

variable "session_token_expiration" {
  description = "Session (access) token lifetime"
  type        = string
  default     = "10 minutes"
}

variable "refresh_token_expiration" {
  description = "Refresh token lifetime"
  type        = string
  default     = "4 weeks"
}

variable "enable_inactivity" {
  description = "Expire refresh tokens after a period of inactivity"
  type        = bool
  default     = true
}

variable "inactivity_time" {
  description = "Inactivity timeout (must be >= 10 minutes)"
  type        = string
  default     = "30 minutes"
}
