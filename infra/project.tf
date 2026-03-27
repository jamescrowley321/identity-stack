# Import the existing Descope project so Terraform manages it without
# creating a duplicate (Descope licensing limits the number of projects).
#
# After the shared project is created, set the project ID:
#   terraform apply -var descope_project_id=P3xxx...

variable "descope_project_id" {
  description = "ID of the shared Descope project (from py-identity-model Terraform)"
  type        = string
}

import {
  to = descope_project.main
  id = var.descope_project_id
}

resource "descope_project" "main" {
  name = var.descope_project_name

  lifecycle {
    prevent_destroy = true
  }

  project_settings {
    refresh_token_rotation              = true
    session_token_expiration            = var.session_token_expiration
    refresh_token_expiration            = var.refresh_token_expiration
    access_key_session_token_expiration = "3 minutes"
    enable_inactivity                   = var.enable_inactivity
    inactivity_time                     = var.inactivity_time
  }

  # Social login providers — disabled by default; supply credentials via tfvars
  # to enable. Descope handles the OAuth dance and issues OIDC tokens to the app.
  authentication {
    oauth {
      system {
        google {
          client_id            = var.google_oauth_client_id
          client_secret        = var.google_oauth_client_secret
          disabled             = var.google_oauth_client_id == "" || var.google_oauth_client_secret == ""
          merge_user_accounts  = true
          allowed_grant_types  = ["authorization_code"]
        }
        github {
          client_id            = var.github_oauth_client_id
          client_secret        = var.github_oauth_client_secret
          disabled             = var.github_oauth_client_id == "" || var.github_oauth_client_secret == ""
          merge_user_accounts  = true
          allowed_grant_types  = ["authorization_code"]
        }
      }
    }
  }
}
