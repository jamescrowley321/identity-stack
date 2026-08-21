variable "project_name" {
  description = "Display name of the Ory project (shown in the Ory Console)."
  type        = string
}

variable "environment" {
  description = "Ory project environment: dev, stage, or prod."
  type        = string
  default     = "dev"
  validation {
    condition     = contains(["dev", "stage", "prod"], var.environment)
    error_message = "environment must be one of: dev, stage, prod."
  }
}

variable "home_region" {
  description = "Ory project home region (immutable after creation)."
  type        = string
  default     = "eu-central"
}

variable "access_token_strategy" {
  description = "OAuth2 access-token strategy. 'jwt' lets identity-stack validate tokens locally via py-identity-model (the strategy PIM already relies on)."
  type        = string
  default     = "jwt"
  validation {
    condition     = contains(["jwt", "opaque"], var.access_token_strategy)
    error_message = "access_token_strategy must be 'jwt' or 'opaque'."
  }
}

variable "allowed_top_level_claims" {
  description = "Non-standard claims allowed as top-level fields in access tokens. Empty for the canonical-side default (no custom claims needed)."
  type        = list(string)
  default     = []
}

variable "enforce_pkce_public_clients" {
  description = "Require PKCE for public clients (baseline OIDC hardening; NFR-1)."
  type        = bool
  default     = true
}

variable "spa_client_name" {
  description = "Display name for the public SPA OAuth2 client."
  type        = string
}

variable "spa_redirect_uris" {
  description = "Allowed redirect URIs for the SPA authorization-code + PKCE flow."
  type        = list(string)
}

variable "spa_post_logout_redirect_uris" {
  description = "Allowed post-logout redirect URIs for OIDC RP-initiated logout."
  type        = list(string)
  default     = []
}

variable "spa_scope" {
  description = "Space-delimited scopes granted to the SPA client."
  type        = string
  default     = "openid profile email offline_access"
}

variable "spa_grant_types" {
  description = "OAuth2 grant types for the SPA client."
  type        = list(string)
  default     = ["authorization_code", "refresh_token"]
}

variable "spa_audience" {
  description = "Allowed access-token audience(s) for the SPA client. The backend enforces this (ORY_AUDIENCE), and the SPA must request it so tokens carry a matching aud."
  type        = list(string)
  default     = []
}

variable "spa_allowed_cors_origins" {
  description = "Browser origins allowed to call Ory's OAuth2 endpoints (the SPA origin(s)) — required for the browser PKCE token exchange."
  type        = list(string)
  default     = []
}

variable "project_api_key_expires_at" {
  description = "Optional RFC3339 expiry for the Terraform-managed project API key. null = non-expiring (rotate manually)."
  type        = string
  default     = null
}

variable "identity_schema_id" {
  description = "Stable id for the project's default identity schema."
  type        = string
}

variable "identity_schema_title" {
  description = "Human title for the identity schema."
  type        = string
  default     = "User"
}

variable "project_api_key_name" {
  description = "Name for the Terraform-managed project API key used to provision project-scoped resources (OAuth2 clients) and, later, backend admin sync."
  type        = string
  default     = "terraform-managed"
}

variable "enable_organizations" {
  description = "Provision an Ory Organization (B2B). Default false = canonical-side tenancy (Organizations is a paid feature; the Develop tier does not include it)."
  type        = bool
  default     = false
}

variable "organization_label" {
  description = "Label for the example Ory Organization (only used when enable_organizations = true)."
  type        = string
  default     = ""
}
