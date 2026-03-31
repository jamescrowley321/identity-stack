# Access keys for programmatic API access.
# Keys can be global or scoped to specific tenants with roles.

resource "descope_access_key" "integration_tests" {
  name        = "Integration Tests"
  description = "Access key used by CI integration tests for client credentials flow"
}

# Example: tenant-scoped access key with viewer role
resource "descope_access_key" "acme_api" {
  name        = "Acme API Key"
  description = "Programmatic access for Acme Corp integrations"

  key_tenants = [{
    tenant_id = descope_tenant.acme.id
    roles     = [descope_role.viewer.name]
  }]
}
