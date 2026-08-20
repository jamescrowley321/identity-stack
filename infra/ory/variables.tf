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
