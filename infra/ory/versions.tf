terraform {
  required_version = ">= 1.9"
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
