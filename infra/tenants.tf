# Default tenants for the SaaS starter.
# Add more tenants by following this pattern.
# Users are assigned to tenants through the Descope console or Management API.

resource "descope_tenant" "acme" {
  name = "Acme Corp"

  custom_attributes = {
    plan_tier   = "pro"
    max_members = "50"
  }
}

resource "descope_tenant" "globex" {
  name = "Globex Inc"

  custom_attributes = {
    plan_tier   = "free"
    max_members = "10"
  }
}
