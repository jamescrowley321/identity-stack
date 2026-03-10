variable "descope_project_name" {
  description = "Name for the Descope project"
  type        = string
  default     = "SaaS Starter"
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
