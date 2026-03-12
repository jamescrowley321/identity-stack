# The Descope project is managed by py-identity-model/infra/descope.
# This config references the shared project by ID to avoid creating
# duplicates (Descope licensing limits the number of projects).
#
# After the shared project is created, set the project ID:
#   terraform apply -var descope_project_id=P3xxx...

variable "descope_project_id" {
  description = "ID of the shared Descope project (from py-identity-model Terraform)"
  type        = string
}
