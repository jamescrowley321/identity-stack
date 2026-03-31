# Push Descope credentials to GitHub Actions so CI stays in sync
# whenever resources are recreated.

resource "github_actions_secret" "descope_project_id" {
  repository      = var.github_repository
  secret_name     = "DESCOPE_PROJECT_ID"
  plaintext_value = var.descope_project_id
}

resource "github_actions_secret" "descope_client_id" {
  repository      = var.github_repository
  secret_name     = "DESCOPE_CLIENT_ID"
  plaintext_value = descope_access_key.integration_tests.client_id
}

resource "github_actions_secret" "descope_client_secret" {
  repository      = var.github_repository
  secret_name     = "DESCOPE_CLIENT_SECRET"
  plaintext_value = descope_access_key.integration_tests.cleartext
}

# Generate an expired access-key token for negative test cases.
# The project's access_key_session_token_expiration is 3 minutes,
# so any token created here will be expired by the time CI runs.
resource "terraform_data" "expired_token" {
  triggers_replace = [
    descope_access_key.integration_tests.client_id,
    descope_access_key.integration_tests.cleartext,
  ]

  provisioner "local-exec" {
    command = <<-EOT
      TOKEN=$(curl -s -X POST https://api.descope.com/v1/auth/accesskey/exchange \
        -H "Authorization: Bearer ${var.descope_project_id}:${descope_access_key.integration_tests.cleartext}" \
        -H "Content-Type: application/json" \
        -d '{"loginId": "${descope_access_key.integration_tests.client_id}"}' \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('sessionJwt',''))")

      echo "$TOKEN" > ${path.module}/expired_token.txt
    EOT
  }
}

data "local_file" "expired_token" {
  depends_on = [terraform_data.expired_token]
  filename   = "${path.module}/expired_token.txt"
}

resource "github_actions_secret" "descope_expired_token" {
  repository      = var.github_repository
  secret_name     = "DESCOPE_EXPIRED_TOKEN"
  plaintext_value = trimspace(data.local_file.expired_token.content)
}

resource "github_actions_secret" "descope_management_key" {
  repository      = var.github_repository
  secret_name     = "DESCOPE_MANAGEMENT_KEY"
  plaintext_value = var.descope_management_key
}

resource "github_actions_secret" "e2e_test_email" {
  repository      = var.github_repository
  secret_name     = "E2E_TEST_EMAIL"
  plaintext_value = var.e2e_test_email
}

resource "github_actions_secret" "e2e_test_tenant_id" {
  repository      = var.github_repository
  secret_name     = "E2E_TEST_TENANT_ID"
  plaintext_value = descope_tenant.acme.id
}
