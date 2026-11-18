# The URLs every local provider root takes, and the one place the plain-HTTP
# rule is written: http:// only for a loopback or *.localhost host. The backend
# accepts <PROVIDER>_REQUIRE_HTTPS=false only for such issuers. A root reads its
# URLs from this module's outputs, never from its own variables, so nothing
# (the provider block included) uses a URL before these validations pass.

variable "issuer_url" {
  description = "The provider's issuer, exactly as in a token's iss."
  type        = string

  validation {
    condition     = can(regex("^https?://[^/?#]+(/[^?#]*)?$", var.issuer_url)) && !endswith(var.issuer_url, "/")
    error_message = "issuer_url must be an http(s) URL with no query, fragment or trailing slash: it is compared with the token's iss as written."
  }

  validation {
    condition     = startswith(var.issuer_url, "https://") || can(regex(local.loopback_url, var.issuer_url))
    error_message = "issuer_url may use http:// only for a loopback or *.localhost host; any other issuer needs https://."
  }
}

variable "frontend_url" {
  description = "Origin of the SPA: the app clients' redirect URI and web origin. The post-logout redirect is <frontend_url>/login."
  type        = string

  validation {
    condition     = can(regex("^https?://([A-Za-z0-9.-]+|\\[[0-9A-Fa-f:.]+\\])(:[0-9]+)?$", var.frontend_url))
    error_message = "frontend_url must be a bare origin, scheme://host[:port] with no path, query or trailing slash: it is the clients' web origin, and the post-logout redirect is <frontend_url>/login."
  }

  validation {
    condition     = startswith(var.frontend_url, "https://") || can(regex(local.loopback_url, var.frontend_url))
    error_message = "frontend_url may use http:// only for a loopback or *.localhost host; any other origin needs https://."
  }
}

variable "tls_optional" {
  description = "Whether the root lets the provider serve its clients without TLS (Keycloak ssl_required = \"none\")."
  type        = bool
  default     = false
  nullable    = false

  validation {
    condition     = !var.tls_optional || can(regex(local.loopback_url, var.issuer_url))
    error_message = "TLS may be optional only for a local provider (Keycloak ssl_required = \"none\"): issuer_url must be a loopback or *.localhost host, or require TLS."
  }
}

locals {
  # An http(s) URL whose host is loopback (localhost, 127.0.0.1, [::1]) or a
  # *.localhost name, with an optional port and path.
  loopback_url = "^https?://(localhost|127\\.0\\.0\\.1|\\[::1\\]|[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)*\\.localhost)(:[0-9]+)?(/.*)?$"
}
