variable "spa_redirect_uris" {
  description = "SPA redirect URIs (auth_code + PKCE). Dev default targets the Vite dev server origin."
  type        = list(string)
  default     = ["http://localhost:3000", "http://localhost:3000/callback"]
}

variable "spa_post_logout_redirect_uris" {
  description = "SPA post-logout redirect URIs (OIDC RP-initiated logout)."
  type        = list(string)
  default     = ["http://localhost:3000"]
}

variable "spa_audience" {
  description = "Access-token audience the SPA requests and the backend enforces (ORY_AUDIENCE)."
  type        = list(string)
  default     = ["https://identity-stack-api"]
}

variable "spa_allowed_cors_origins" {
  description = "Browser origins allowed to call Ory OAuth2 endpoints (the SPA origin)."
  type        = list(string)
  default     = ["http://localhost:3000"]
}
