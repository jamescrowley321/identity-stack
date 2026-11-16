terraform {
  required_version = ">= 1.9"

  # No backend block: state is local (terraform.tfstate in this directory) and
  # git-ignored. This root targets the compose Keycloak only.

  required_providers {
    keycloak = {
      source  = "keycloak/keycloak"
      version = "~> 5.10"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# The provider logs in to the master realm as the compose bootstrap admin. The
# username and password come from the environment (KEYCLOAK_USER,
# KEYCLOAK_PASSWORD), never from a tfvars file: see README.md.
provider "keycloak" {
  url       = var.keycloak_url
  realm     = "master"
  client_id = "admin-cli"
}
