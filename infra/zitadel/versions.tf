terraform {
  required_version = ">= 1.9"

  # No backend block: state is local (terraform.tfstate in this directory) and
  # git-ignored. This root targets the compose Zitadel only; the instance is
  # disposable and a remote run could not reach zitadel.localhost.

  required_providers {
    zitadel = {
      source  = "zitadel/zitadel"
      version = "~> 3.8"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# The provider calls the instance as the bootstrap admin machine user
# (terraform-admin, IAM_OWNER) with its personal access token. The token comes
# from the environment (TF_VAR_zitadel_access_token), never from a tfvars file:
# see README.md.
provider "zitadel" {
  domain       = local.zitadel_host
  port         = local.zitadel_port
  insecure     = local.zitadel_insecure
  access_token = var.zitadel_access_token
}
