variable "keycloak_url" {
  description = "Base URL of the Keycloak to build the realm in (its KC_HOSTNAME). The compose `keycloak` service is http://keycloak.localhost:8180."
  type        = string
  default     = "http://keycloak.localhost:8180"

  validation {
    condition     = !endswith(var.keycloak_url, "/")
    error_message = "keycloak_url must not end in a slash: the issuer is <keycloak_url>/realms/<realm>."
  }
}

variable "realm" {
  description = "Name of the realm to create. It must not exist yet. The compose import uses identity-stack, so a container that imported it needs another name here."
  type        = string
  default     = "identity-stack"
}

variable "frontend_url" {
  description = "Origin of the SPA: the app clients' redirect URI and web origin. The post-logout redirect is <frontend_url>/login."
  type        = string
  default     = "http://localhost:3000"

  validation {
    condition     = !endswith(var.frontend_url, "/")
    error_message = "frontend_url must not end in a slash: it is an origin, and the post-logout redirect is <frontend_url>/login."
  }
}

variable "ssl_required" {
  description = "Realm SSL mode. `none` matches the compose import and is for a local container only."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "external", "all"], var.ssl_required)
    error_message = "ssl_required must be none, external or all."
  }
}
