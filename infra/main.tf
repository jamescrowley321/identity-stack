terraform {
  cloud {
    organization = "jamescrowley321"

    workspaces {
      name = "identity-stack-dev"
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
