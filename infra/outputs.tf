output "project_id" {
  description = "The Descope project ID"
  value       = var.descope_project_id
}

output "integration_test_access_key_id" {
  description = "Access key ID for integration tests"
  value       = descope_access_key.integration_tests.client_id
}

output "integration_test_access_key_cleartext" {
  description = "Access key secret for integration tests (client_secret)"
  value       = descope_access_key.integration_tests.cleartext
  sensitive   = true
}
