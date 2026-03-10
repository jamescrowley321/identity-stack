# Descope project configuration
# Each phase will add blocks here as features are implemented.

resource "descope_project" "starter" {
  name = var.descope_project_name

  # Phase 1b: Authentication methods
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

  # Phase 2b: Roles and permissions will be configured here
  # authorization = { ... }

  # Phase 2c: Custom attributes will be configured here
  # attributes = { ... }

  # Phase 1c: Session/token settings will be configured here
  # project_settings = { ... }

  # Phase 4b: JWT templates will be configured here
  # jwt_templates = { ... }

  # Phase 5b: Connectors will be configured here
  # connectors = { ... }

  # Phase 5d: OIDC application for client credentials flow
  applications = {
    oidc_applications = [
      {
        name        = "Integration Tests"
        description = "OIDC application used by CI integration tests"
      }
    ]
  }
}
