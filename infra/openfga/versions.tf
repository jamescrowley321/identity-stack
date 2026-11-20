terraform {
  required_version = ">= 1.9"

  # No backend block: state is local (terraform.tfstate in this directory) and
  # git-ignored. This root targets the compose OpenFGA only.

  required_providers {
    openfga = {
      source  = "openfga/openfga"
      version = "~> 0.5.1"
    }
  }
}

# No authentication: the compose servers listen on loopback only. A server that
# requires a pre-shared key takes it from FGA_API_TOKEN, never from a tfvars file.
provider "openfga" {
  api_url = var.api_url
}
