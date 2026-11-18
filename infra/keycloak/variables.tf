variable "issuer_url" {
  description = "Issuer of the realm to create, <Keycloak base URL>/realms/<realm>: the base URL is the Keycloak's KC_HOSTNAME and the realm must not exist yet. The default is the compose `keycloak` service with a realm beside the one the compose import ships (identity-stack); name realm identity-stack only for a Keycloak that did not import it. http:// only for a loopback or *.localhost host (infra/modules/provider-urls)."
  type        = string
  default     = "http://keycloak.localhost:8180/realms/identity-stack-terraform"

  validation {
    condition     = can(regex(local.realm_issuer, var.issuer_url))
    error_message = "issuer_url must be <Keycloak base URL>/realms/<realm>, with no trailing slash and no slash before /realms/: it is the realm's issuer, compared with the token's iss as written."
  }
}

variable "frontend_url" {
  description = "Origin of the SPA: the app clients' redirect URI and web origin. The post-logout redirect is <frontend_url>/login. http:// only for a loopback or *.localhost host (infra/modules/provider-urls)."
  type        = string
  default     = "http://localhost:3000"
}

variable "ssl_required" {
  description = "Realm SSL mode. `none` matches the compose import and is accepted only when issuer_url is a loopback or *.localhost host (infra/modules/provider-urls)."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "external", "all"], var.ssl_required)
    error_message = "ssl_required must be none, external or all."
  }
}

# The plain-HTTP rule shared by every local provider root. Everything below
# reads the URLs from here.
module "urls" {
  source = "../modules/provider-urls"

  issuer_url   = var.issuer_url
  frontend_url = var.frontend_url
  tls_optional = var.ssl_required == "none"
}

locals {
  # A Keycloak issuer: the base URL (not ending in /, so never //realms/), then
  # /realms/ and the realm name.
  realm_issuer = "^(.*[^/])/realms/([A-Za-z0-9._-]+)$"

  issuer_url   = module.urls.issuer_url
  frontend_url = module.urls.frontend_url
  keycloak_url = regex(local.realm_issuer, local.issuer_url)[0]
  realm        = regex(local.realm_issuer, local.issuer_url)[1]
}
