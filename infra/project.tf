# Consolidated Descope project configuration.
# This is the single shared project used by all repos:
#   - descope-saas-starter (authentication, OIDC app)
#   - py-identity-model (authorization, project settings)
#   - terraform-provider-descope (CI integration tests)

resource "descope_project" "starter" {
  name = var.descope_project_name

  project_settings = {
    access_key_session_token_expiration = "3 minutes"
  }

  authentication = {
    otp = {
      disabled        = false
      expiration_time = "5 minutes"
    }

    magic_link = {
      disabled        = false
      expiration_time = "10 minutes"
    }

    password = {
      disabled                = false
      min_length              = 8
      uppercase               = true
      lowercase               = true
      number                  = true
      non_alphanumeric        = false
      lock                    = true
      lock_attempts           = 5
      temporary_lock          = true
      temporary_lock_attempts = 3
      temporary_lock_duration = "5 minutes"
    }

    oauth = {
      disabled = false
      system = {
        google = {
          disabled      = var.google_oauth_client_id == "" ? true : false
          client_id     = var.google_oauth_client_id
          client_secret = var.google_oauth_client_secret
        }
        github = {
          disabled      = var.github_oauth_client_id == "" ? true : false
          client_id     = var.github_oauth_client_id
          client_secret = var.github_oauth_client_secret
        }
      }
    }

    passkeys = {
      disabled = false
    }

    totp = {
      disabled      = false
      service_label = "Descope SaaS Starter"
    }
  }

  authorization = {
    permissions = [
      { name = "users.create", description = "Create users" },
      { name = "users.read", description = "Read users" },
      { name = "users.delete", description = "Delete users" },
    ]
    roles = [
      {
        name        = "admin"
        key         = "admin"
        description = "Full administrative access"
        permissions = ["users.create", "users.read", "users.delete"]
      },
      {
        name        = "viewer"
        key         = "viewer"
        description = "Read-only access"
        permissions = ["users.read"]
      },
    ]
  }

  applications = {
    oidc_applications = [
      {
        name           = "Integration Tests"
        description    = "OIDC application used by CI integration tests"
        login_page_url = "https://localhost"
      }
    ]
  }
}
