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
