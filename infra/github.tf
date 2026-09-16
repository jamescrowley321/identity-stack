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

# A permanently-expired access-key token, for the negative auth tests
# (backend/tests/integration/test_api.py, test_session.py).
#
# This used to be minted here by a `local-exec` provisioner that wrote
# infra/expired_token.txt, which a `data "local_file"` then read back. That
# could not survive anything but a run started from a workstation that happened
# to have the file: the provisioner runs on APPLY, the data source is read on
# PLAN, and HCP Terraform's run environment is rebuilt from the uploaded
# configuration every run. Since the file is gitignored, a plan only succeeded
# because the CLI uploaded it from one particular local directory, and a
# VCS-driven run — which clones the repo instead — would fail with
# "file cannot be read".
#
# Regenerating it was never necessary anyway: a token that has expired stays
# expired, so this is a static fixture, not derived infrastructure. It is now an
# ordinary sensitive input, guarded the same way the other CI secrets are.
resource "github_actions_secret" "descope_expired_token" {
  repository      = var.github_repository
  secret_name     = "DESCOPE_EXPIRED_TOKEN"
  plaintext_value = var.descope_expired_token

  # Same fail-loud guard as the other credential pushes: refuse to overwrite a
  # live CI secret with a blank value rather than silently disabling the three
  # negative-auth tests that consume it.
  lifecycle {
    precondition {
      condition     = length(trimspace(var.descope_expired_token)) > 0
      error_message = "Refusing to push an empty DESCOPE_EXPIRED_TOKEN secret — set descope_expired_token."
    }
  }
}

# Sourced from the TF-minted management key (always valid, always populated) —
# never from var.descope_management_key (defaulted "", silently blanked the
# secret; a hand-filled stale .env key 401'd the mgmt API).
resource "github_actions_secret" "descope_management_key" {
  repository      = var.github_repository
  secret_name     = "DESCOPE_MANAGEMENT_KEY"
  plaintext_value = descope_management_key.ci_e2e.cleartext
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
