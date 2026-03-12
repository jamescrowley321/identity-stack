resource "descope_access_key" "integration_tests" {
  name        = "Integration Tests"
  description = "Access key used by CI integration tests for client credentials flow"
}
