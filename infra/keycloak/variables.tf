variable "keycloak_url" {
  description = "Base URL of the Keycloak to build the realm in (its KC_HOSTNAME). The compose `keycloak` service is http://keycloak.localhost:8180."
  type        = string
  default     = "http://keycloak.localhost:8180"

  validation {
    condition     = !endswith(var.keycloak_url, "/")
    error_message = "keycloak_url must not end in a slash: the issuer is <keycloak_url>/realms/<realm>."
  }

  validation {
    condition     = startswith(var.keycloak_url, "https://") || can(regex(local.loopback_url, var.keycloak_url))
    error_message = "keycloak_url may use http:// only for a loopback or *.localhost host; any other Keycloak needs https://."
  }
}

variable "realm" {
  description = "Name of the realm to create. It must not exist yet. The default sits beside the realm the compose import ships (identity-stack); set realm = \"identity-stack\" only for a Keycloak that did not import it."
  type        = string
  default     = "identity-stack-terraform"
}

variable "frontend_url" {
  description = "Origin of the SPA: the app clients' redirect URI and web origin. The post-logout redirect is <frontend_url>/login."
  type        = string
  default     = "http://localhost:3000"

  validation {
    condition     = can(regex("^https?://[^/?#]+$", var.frontend_url))
    error_message = "frontend_url must be a bare origin, scheme://host[:port] with no path, query or trailing slash: it is the clients' web origin, and the post-logout redirect is <frontend_url>/login."
  }

  validation {
    condition     = startswith(var.frontend_url, "https://") || can(regex(local.loopback_url, var.frontend_url))
    error_message = "frontend_url may use http:// only for a loopback or *.localhost host; any other origin needs https://."
  }
}

variable "ssl_required" {
  description = "Realm SSL mode. `none` matches the compose import and is accepted only when keycloak_url is a loopback or *.localhost host."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "external", "all"], var.ssl_required)
    error_message = "ssl_required must be none, external or all."
  }

  validation {
    condition     = var.ssl_required != "none" || can(regex(local.loopback_url, var.keycloak_url))
    error_message = "ssl_required = \"none\" is for a local Keycloak only: keycloak_url must be a loopback or *.localhost host, or set ssl_required to external or all."
  }
}

locals {
  # An http(s) URL whose host is loopback (localhost, 127.0.0.1, [::1]) or a
  # *.localhost name, with an optional port and path. The only hosts where the
  # root accepts plain http and ssl_required = "none", as the backend accepts
  # KEYCLOAK_REQUIRE_HTTPS=false only for such issuers.
  loopback_url = "^https?://(localhost|127\\.0\\.0\\.1|\\[::1\\]|[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)*\\.localhost)(:[0-9]+)?(/.*)?$"
}
