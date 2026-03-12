# The Descope project is created externally (py-identity-model/infra/descope)
# to avoid duplicates (Descope licensing limits the number of projects).
#
# This config imports the existing project to manage session settings.
# See session.tf for the descope_project resource.
#
# First-time setup:
#   terraform import descope_project.main <project_id>
#   terraform apply -var descope_project_id=<project_id>

variable "descope_project_id" {
  description = "ID of the shared Descope project (from py-identity-model Terraform)"
  type        = string
}
