# The realm the compose import ships (keycloak/realm/identity-stack-realm.json),
# described in Terraform. Anything added to the import file is added here too.
#
# Model: an organization is a tenant. A "type client" is a confidential client
# with no flows that holds client roles (the permissions). Each type client has
# one public "app client" the SPA logs in through, scope-mapped to the type
# client's roles only, so a token's aud is the type client and its
# resource_access holds that type client's roles alone.

locals {
  # Type clients, their client roles, and the app client for each. `portable`
  # and `portable_permissions` are the backend's token model for the type
  # (KEYCLOAK_PORTABLE_TYPES, KEYCLOAK_ORGANIZATION_BOUND_TYPES,
  # KEYCLOAK_PORTABLE_PERMISSIONS); the realm itself does not read them.
  type_clients = {
    "member" = {
      name                 = "Type client: member"
      app_client           = "member-app"
      app_name             = "App client for type member"
      roles                = ["documents:read", "documents:write"]
      portable             = true
      portable_permissions = ["documents:read"]
    }
    "tenant-admin" = {
      name                 = "Type client: tenant-admin"
      app_client           = "tenant-admin-app"
      app_name             = "App client for type tenant-admin"
      roles                = ["members:invite", "members:remove", "settings:manage"]
      portable             = false
      portable_permissions = []
    }
  }

  client_roles = merge([
    for type_client, spec in local.type_clients : {
      for role in spec.roles : "${type_client}/${role}" => {
        type_client = type_client
        name        = role
      }
    }
  ]...)

  # Fixture users. Keycloak picks their ids: the provider cannot set one, so
  # a token's sub differs from the import's fixed ids. Client roles are
  # "<type client>/<role>" keys of local.client_roles.
  users = {
    alice = { email = "alice@acme.test", first_name = "Alice", last_name = "Single-Org", roles = ["member/documents:read", "member/documents:write"] }
    bob = { email = "bob@globex.test", first_name = "Bob", last_name = "Two-Orgs", roles = [
      "member/documents:read",
      "tenant-admin/members:invite", "tenant-admin/members:remove", "tenant-admin/settings:manage",
    ] }
    carol = { email = "carol@acme.test", first_name = "Carol", last_name = "No-Role", roles = [] }
    dave = { email = "dave@acme.test", first_name = "Dave", last_name = "One-Org-Admin", roles = [
      "tenant-admin/members:invite", "tenant-admin/members:remove", "tenant-admin/settings:manage",
    ] }
    erin = { email = "erin@acme.test", first_name = "Erin", last_name = "Write-Only", roles = ["member/documents:write"] }
    frank = { email = "frank@acme.test", first_name = "Frank", last_name = "Moving-Admin", roles = [
      "tenant-admin/members:invite", "tenant-admin/members:remove", "tenant-admin/settings:manage",
    ] }
  }

  # Organizations. `type_holders` is the organization's record of which
  # members hold an organization-bound type there: attribute
  # type:<type client> listing their user ids, read by the backend's
  # directory lookup.
  organizations = {
    acme = {
      name         = "Acme"
      domain       = "acme.test"
      members      = ["alice", "bob", "carol", "dave", "erin", "frank"]
      type_holders = { "tenant-admin" = ["bob", "dave", "frank"] }
    }
    globex = {
      name         = "Globex"
      domain       = "globex.test"
      members      = ["bob"]
      type_holders = {}
    }
  }

  # The backend's read-only service account for the organization lookup.
  directory_client_id    = "identity-stack-directory"
  directory_client_roles = ["view-users", "view-organizations"]

  # The provider's separator for a multi-valued attribute in a map(string).
  multivalue_separator = "##"
}

resource "keycloak_realm" "this" {
  realm                    = var.realm
  enabled                  = true
  ssl_required             = var.ssl_required
  registration_allowed     = false
  duplicate_emails_allowed = false
  # The backend links a Keycloak identity to an existing canonical user on a
  # verified email (docs/security.md), so the realm verifies emails.
  verify_email          = true
  organizations_enabled = true

  # Brute-force detection on, with temporary lockouts (Keycloak's own numbers),
  # as the import sets it: a guessed password locks the user out for a while.
  security_defenses {
    brute_force_detection {
      permanent_lockout                = false
      max_login_failures               = 30
      wait_increment_seconds           = 60
      quick_login_check_milli_seconds  = 1000
      minimum_quick_login_wait_seconds = 60
      max_failure_wait_seconds         = 900
      failure_reset_time_seconds       = 43200
    }
  }
}

# --- Type clients and their client roles -------------------------------------

resource "keycloak_openid_client" "type" {
  for_each = local.type_clients

  realm_id                     = keycloak_realm.this.id
  client_id                    = each.key
  name                         = each.value.name
  enabled                      = true
  access_type                  = "CONFIDENTIAL"
  standard_flow_enabled        = false
  implicit_flow_enabled        = false
  direct_access_grants_enabled = false
  service_accounts_enabled     = false
  full_scope_allowed           = false
  # "+": the redirect URIs, of which there are none. What the import sets.
  valid_post_logout_redirect_uris = ["+"]
}

resource "keycloak_role" "client" {
  for_each = local.client_roles

  realm_id  = keycloak_realm.this.id
  client_id = keycloak_openid_client.type[each.value.type_client].id
  name      = each.value.name
}

# --- App clients -----------------------------------------------------------------

resource "keycloak_openid_client" "app" {
  for_each = local.type_clients

  realm_id                        = keycloak_realm.this.id
  client_id                       = each.value.app_client
  name                            = each.value.app_name
  enabled                         = true
  access_type                     = "PUBLIC"
  standard_flow_enabled           = true
  implicit_flow_enabled           = false
  direct_access_grants_enabled    = false
  service_accounts_enabled        = false
  full_scope_allowed              = false
  pkce_code_challenge_method      = "S256"
  valid_redirect_uris             = [var.frontend_url]
  web_origins                     = [var.frontend_url]
  valid_post_logout_redirect_uris = ["${var.frontend_url}/login"]
}

resource "keycloak_openid_client_default_scopes" "app" {
  for_each = local.type_clients

  realm_id       = keycloak_realm.this.id
  client_id      = keycloak_openid_client.app[each.key].id
  default_scopes = ["basic", "roles", "profile", "email", "web-origins", "acr"]
}

resource "keycloak_openid_client_optional_scopes" "app" {
  for_each = local.type_clients

  realm_id        = keycloak_realm.this.id
  client_id       = keycloak_openid_client.app[each.key].id
  optional_scopes = ["organization", "offline_access"]
}

# Scope mapping: each app client may put its own type client's roles, and no
# other role, in a token.
resource "keycloak_generic_role_mapper" "app" {
  for_each = local.client_roles

  realm_id  = keycloak_realm.this.id
  client_id = keycloak_openid_client.app[each.value.type_client].id
  role_id   = keycloak_role.client[each.key].id
}

# --- Users -----------------------------------------------------------------------

# Login passwords for the fixture users, generated per apply and kept in local
# state only (output `user_passwords`). None is committed.
resource "random_password" "user" {
  for_each = local.users

  length  = 24
  special = false
}

resource "keycloak_user" "this" {
  for_each = local.users

  realm_id       = keycloak_realm.this.id
  username       = each.key
  enabled        = true
  email          = each.value.email
  email_verified = true
  first_name     = each.value.first_name
  last_name      = each.value.last_name

  initial_password {
    value     = random_password.user[each.key].result
    temporary = false
  }
}

# Authoritative over each user's roles: a user holds exactly the client roles
# listed and no realm role, as in the import (Keycloak gives a user created
# through the Admin API the realm's default role; the import does not).
resource "keycloak_user_roles" "this" {
  for_each = local.users

  realm_id   = keycloak_realm.this.id
  user_id    = keycloak_user.this[each.key].id
  role_ids   = [for role in each.value.roles : keycloak_role.client[role].id]
  exhaustive = true
}

# --- Organizations ---------------------------------------------------------------

resource "keycloak_organization" "this" {
  for_each = local.organizations

  realm   = keycloak_realm.this.id
  name    = each.value.name
  alias   = each.key
  enabled = true

  domain {
    name     = each.value.domain
    verified = false
  }

  attributes = {
    for type_client, holders in each.value.type_holders :
    "type:${type_client}" => join(local.multivalue_separator, [for username in holders : keycloak_user.this[username].id])
  }
}

# Authoritative: a member added to an organization by hand is removed on the
# next apply.
resource "keycloak_organization_memberships" "this" {
  for_each = local.organizations

  realm_id        = keycloak_realm.this.id
  organization_id = keycloak_organization.this[each.key].id
  members         = [for username in each.value.members : keycloak_user.this[username].username]
}

# --- Directory service account ---------------------------------------------------

data "keycloak_openid_client" "realm_management" {
  realm_id  = keycloak_realm.this.id
  client_id = "realm-management"
}

data "keycloak_role" "realm_management" {
  for_each = toset(local.directory_client_roles)

  realm_id  = keycloak_realm.this.id
  client_id = data.keycloak_openid_client.realm_management.id
  name      = each.key
}

resource "keycloak_openid_client" "directory" {
  realm_id                     = keycloak_realm.this.id
  client_id                    = local.directory_client_id
  name                         = "Backend service account: organization lookup, read-only"
  enabled                      = true
  access_type                  = "CONFIDENTIAL"
  standard_flow_enabled        = false
  implicit_flow_enabled        = false
  direct_access_grants_enabled = false
  service_accounts_enabled     = true
  full_scope_allowed           = false
  # "+": the redirect URIs, of which there are none. What the import sets.
  valid_post_logout_redirect_uris = ["+"]
}

# Without kc-admin-api the service account's token lacks the Admin API
# audience and every Admin API call is 401.
resource "keycloak_openid_client_default_scopes" "directory" {
  realm_id       = keycloak_realm.this.id
  client_id      = keycloak_openid_client.directory.id
  default_scopes = ["kc-admin-api"]
}

resource "keycloak_openid_client_optional_scopes" "directory" {
  realm_id        = keycloak_realm.this.id
  client_id       = keycloak_openid_client.directory.id
  optional_scopes = []
}

# Authoritative over the service account's roles: exactly the realm-management
# roles listed and no realm role, as in the import (Keycloak gives a new
# service account the realm's default role, as it does a user).
resource "keycloak_user_roles" "directory" {
  realm_id   = keycloak_realm.this.id
  user_id    = keycloak_openid_client.directory.service_account_user_id
  role_ids   = [for role in local.directory_client_roles : data.keycloak_role.realm_management[role].id]
  exhaustive = true
}

resource "keycloak_generic_role_mapper" "directory" {
  for_each = toset(local.directory_client_roles)

  realm_id  = keycloak_realm.this.id
  client_id = keycloak_openid_client.directory.id
  role_id   = data.keycloak_role.realm_management[each.key].id
}
