variable "api_url" {
  description = "Base URL of the OpenFGA HTTP API: the compose `openfga` service (http://localhost:8380) by default, `openfga-test` (http://localhost:18380) for make openfga-terraform-check and CI."
  type        = string
  default     = "http://localhost:8380"

  validation {
    condition     = can(regex("^https?://[^/]+$", var.api_url))
    error_message = "api_url must be scheme://host[:port], with no path and no trailing slash."
  }
}

variable "store_name" {
  description = "Name of the OpenFGA store to create."
  type        = string
  default     = "identity-stack"
}
