variable "zitadel_url" {
  description = "Base URL of the Zitadel instance, which is also its issuer. The compose `zitadel` profile is http://zitadel.localhost:8480."
  type        = string
  default     = "http://zitadel.localhost:8480"

  validation {
    condition     = can(regex("^https?://[A-Za-z0-9.-]+(:[0-9]+)?$", var.zitadel_url))
    error_message = "zitadel_url must be a bare origin, scheme://host[:port], with no path or trailing slash: it is the issuer."
  }

  validation {
    condition     = startswith(var.zitadel_url, "https://") || can(regex(local.loopback_url, var.zitadel_url))
    error_message = "zitadel_url may use http:// only for a loopback or *.localhost host; any other Zitadel needs https://."
  }
}

variable "zitadel_access_token" {
  description = "Personal access token of an IAM_OWNER machine user. For the compose instance: `export TF_VAR_zitadel_access_token=\"$(make -s zitadel-admin-pat)\"`. Never put it in a tfvars file."
  type        = string
  sensitive   = true
  nullable    = false

  validation {
    condition     = length(var.zitadel_access_token) > 0
    error_message = "zitadel_access_token is empty: export TF_VAR_zitadel_access_token=\"$(make -s zitadel-admin-pat)\" with the local Zitadel running."
  }
}

variable "frontend_url" {
  description = "Origin of the SPA: the applications' redirect URI. The post-logout redirect is <frontend_url>/login."
  type        = string
  default     = "http://localhost:3000"

  validation {
    condition     = can(regex("^https?://([A-Za-z0-9.-]+|\\[[0-9A-Fa-f:.]+\\])(:[0-9]+)?$", var.frontend_url))
    error_message = "frontend_url must be a bare origin, scheme://host[:port] with no path, query or trailing slash."
  }

  validation {
    condition     = startswith(var.frontend_url, "https://") || can(regex(local.loopback_url, var.frontend_url))
    error_message = "frontend_url may use http:// only for a loopback or *.localhost host; any other origin needs https://."
  }
}

variable "access_token_lifetime" {
  description = "Instance access-token lifetime, as a Go duration in the form Zitadel answers it (hours, minutes and seconds, e.g. 15m0s). Zitadel's own default is 12 hours."
  type        = string
  default     = "15m0s"

  validation {
    # Go's canonical form for a whole, non-zero number of seconds: 45s, 15m0s, 1h0m0s.
    # Minutes and seconds below 60, no leading zeros, every unit below the largest
    # written out. Any other spelling (900s, 60m0s, 0h15m0s) is read back
    # differently and the next plan is not empty.
    condition     = can(regex("^([1-9][0-9]*h[1-5]?[0-9]m[1-5]?[0-9]s|([1-9]|[1-5][0-9])m[1-5]?[0-9]s|([1-9]|[1-5][0-9])s)$", var.access_token_lifetime))
    error_message = "access_token_lifetime must be a non-zero Go duration in the form Zitadel answers (like 45s, 15m0s or 1h0m0s), or the next plan is not empty."
  }
}

locals {
  # An http(s) URL whose host is loopback (localhost, 127.0.0.1, [::1]) or a
  # *.localhost name, with an optional port. The only hosts where the root
  # accepts plain http.
  loopback_url = "^https?://(localhost|127\\.0\\.0\\.1|\\[::1\\]|[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)*\\.localhost)(:[0-9]+)?$"

  zitadel_insecure = startswith(var.zitadel_url, "http://")
  zitadel_host     = regex("^https?://([^:/]+)", var.zitadel_url)[0]
  zitadel_port     = try(regex(":([0-9]+)$", var.zitadel_url)[0], local.zitadel_insecure ? "80" : "443")
}
