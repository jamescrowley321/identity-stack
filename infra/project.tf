# Import the existing Descope project so Terraform manages it without
# creating a duplicate (Descope licensing limits the number of projects).
#
# After the shared project is created, set the project ID:
#   terraform apply -var descope_project_id=P3xxx...

variable "descope_project_id" {
  description = "ID of the shared Descope project (from py-identity-model Terraform)"
  type        = string

  validation {
    condition     = length(trimspace(var.descope_project_id)) > 0
    error_message = "descope_project_id must not be empty — set it via TF_VAR_descope_project_id or a workspace variable."
  }
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

  project_settings = {
    refresh_token_rotation              = true
    session_token_expiration            = var.session_token_expiration
    refresh_token_expiration            = var.refresh_token_expiration
    access_key_session_token_expiration = "3 minutes"
    enable_inactivity                   = var.enable_inactivity
    inactivity_time                     = var.inactivity_time
  }
}
