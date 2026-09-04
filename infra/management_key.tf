# TF-owned management key for CI (E2E mgmt-API operations: create users, access
# keys, roles). Synced to the DESCOPE_MANAGEMENT_KEY GitHub secret from this
# resource's generated `cleartext` — the same always-populated pattern as the
# integration-test access key. This replaces sourcing the secret from
# `var.descope_management_key`, which defaulted to "" (blanked the secret) and,
# when hand-filled from a local .env, carried a stale key that 401'd the mgmt API.
resource "descope_management_key" "ci_e2e" {
  name        = "CI E2E (identity-stack)"
  description = "Management key for identity-stack CI E2E tests. Terraform-owned; synced to the DESCOPE_MANAGEMENT_KEY GitHub Actions secret. Rotate by tainting this resource."

  rebac = {
    company_roles = ["company-full-access"]
  }
}
