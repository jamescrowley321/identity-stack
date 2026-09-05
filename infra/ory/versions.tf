terraform {
  required_version = ">= 1.9"

  # Terraform Cloud remote state (org jamescrowley321). Tag-based selection so
  # one config maps to per-environment workspaces (identity-stack-ory-dev,
  # identity-stack-ory-prod). Pick the target at run time:
  #   TF_WORKSPACE=identity-stack-ory-dev terraform plan -var-file=environments/dev.tfvars
  # First-time adoption of the current local state:
  #   1) create the identity-stack-ory-dev workspace (tagged) in TFC
  #   2) set ORY_WORKSPACE_API_KEY (sensitive) + ORY_WORKSPACE_ID as env vars
  #   3) terraform init -migrate-state   # uploads existing local state, no churn
  cloud {
    organization = "jamescrowley321"

    workspaces {
      tags = ["identity-stack", "ory"]
    }
  }

  required_providers {
    ory = {
      source  = "ory/ory"
      version = "~> 26.3"
    }
  }
}

provider "ory" {
  # Auth via env: ORY_WORKSPACE_API_KEY (ory_wak_...) + ORY_WORKSPACE_ID for
  # workspace-scoped operations (creating/configuring projects). Project-scoped
  # admin ops fall back to ORY_PROJECT_API_KEY / ORY_PROJECT_SLUG when set.
}
