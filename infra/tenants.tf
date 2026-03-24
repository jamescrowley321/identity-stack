# Default tenants for the SaaS starter.
# Add more tenants by following this pattern.
# Users are assigned to tenants through the Descope console or Management API.

resource "descope_tenant" "acme" {
  name = "Acme Corp"
}

resource "descope_tenant" "globex" {
  name = "Globex Inc"
}
