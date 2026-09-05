terraform {
  cloud {
    organization = "jamescrowley321"

    # Tag-based selection so one config maps to per-environment workspaces
    # (identity-stack-dev, identity-stack-prod). Pick the target at run time:
    #   TF_WORKSPACE=identity-stack-dev terraform plan -var-file=environments/dev.tfvars
    # The existing identity-stack-dev workspace must carry both tags in TFC;
    # this is an add-tags operation — the workspace is not renamed and state is
    # not moved.
    workspaces {
      tags = ["identity-stack", "descope"]
    }
  }

  required_providers {
    descope = {
      source  = "jamescrowley321/descope"
      version = "~> 1.0"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

provider "descope" {
  project_id = var.descope_project_id
}

provider "github" {
  owner = "jamescrowley321"
}
