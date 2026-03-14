variable "descope_management_key" {
  description = "Descope management key. Set via DESCOPE_MANAGEMENT_KEY env var."
  type        = string
  sensitive   = true
  default     = ""
}

# OAuth2 - Google
variable "google_oauth_client_id" {
  description = "Google OAuth2 client ID (leave empty to disable)"
  type        = string
  default     = ""
}

variable "google_oauth_client_secret" {
  description = "Google OAuth2 client secret"
  type        = string
  default     = ""
  sensitive   = true
}

# OAuth2 - GitHub
variable "github_oauth_client_id" {
  description = "GitHub OAuth2 client ID (leave empty to disable)"
  type        = string
  default     = ""
}

variable "github_oauth_client_secret" {
  description = "GitHub OAuth2 client secret"
  type        = string
  default     = ""
  sensitive   = true
}

# GitHub Actions
variable "github_repository" {
  description = "GitHub repository name (without owner) for CI secrets"
  type        = string
  default     = "descope-saas-starter"
}

# Session settings
variable "descope_project_name" {
  description = "Display name for the Descope project"
  type        = string
  default     = "descope-saas-starter"
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
