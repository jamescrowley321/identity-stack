# The realm the compose import ships (keycloak/realm/identity-stack-realm.json),
# described in Terraform. Anything added to the import file is added here too.
#
# Model: the token says who the caller is; the canonical store (backend
# database) says what the caller may do. An organization is a tenant. The SPA
# logs in through one public client whose access tokens carry the API client's
# id in aud. The API client holds the canonical permissions and roles as client
# roles, mapped to no user and no group, so no token carries one. A role
# assignment (user, tenant, role) is membership of the organization's group
# for the role, a group with no role mapping: the record of the assignment,
# never a grant.

locals {
  # The canonical permissions, <domain>:<action>, and the canonical roles with
  # the permissions each one composes. The API client holds both as client
  # roles; a role is a composite of its permissions.
  permissions = toset([
    "billing:manage",
    "documents:delete",
    "documents:read",
    "documents:write",
    "members:invite",
    "members:remove",
    "members:update_role",
    "projects:create",
    "projects:delete",
    "projects:read",
    "projects:update",
    "settings:manage",
  ])
  roles = {
    owner  = sort(local.permissions)
    admin  = sort(setsubtract(local.permissions, ["billing:manage"]))
    member = ["documents:read", "documents:write", "members:invite", "projects:read", "projects:update"]
    viewer = ["documents:read", "projects:read"]
  }

  spa_client_id   = "identity-stack-spa"
  api_client_id   = "identity-stack-api"
  other_client_id = "identity-stack-other"

  # The public clients a browser logs in through: the SPA, and another one
  # whose tokens lack the API audience (for tests that a token of another
  # client is refused).
  public_clients = {
    (local.spa_client_id)   = "The identity-stack SPA"
    (local.other_client_id) = "Another public client, without the API audience"
  }

  # The test tenants and users (infra/fixtures/test-identities.json): every
  # tenant is an organization, every user a realm user.
  fixture = jsondecode(file("${path.module}/../fixtures/test-identities.json"))

  # Fixture users. Keycloak picks their ids: the provider cannot set one, so
  # a token's sub differs from the import's fixed ids.
  users = {
    for username, user in local.fixture.users : username => {
      email      = "${username}@${local.fixture.tenants[user.home].domain}"
      first_name = user.first_name
      last_name  = user.last_name
    }
  }

  # Each fixture user's canonical role in each tenant, as membership of that
  # organization's group for the role, keyed "<alias>/<role>/<username>".
  role_assignments = merge([
    for username, user in local.fixture.users : {
      for alias, role in user.roles : "${alias}/${role}/${username}" => {
        alias = alias, role = role, username = username
      } if role != null
    }
  ]...)

  # Organizations, one per fixture tenant, with their members.
  organizations = {
    for alias, tenant in local.fixture.tenants : alias => {
      name    = tenant.name
      domain  = tenant.domain
      members = sort([for username, user in local.fixture.users : username if contains(keys(user.roles), alias)])
    }
  }

  # One group per organization and canonical role, owned by the organization.
  role_groups = merge([
    for alias in keys(local.organizations) : {
      for role in keys(local.roles) : "${alias}/${role}" => { alias = alias, role = role }
    }
  ]...)

  # The backend's service account for the Keycloak admin adapter, which pushes
  # canonical changes to the realm and pulls the realm's changes back. Each
  # role is probed in keycloak/fixtures/README.md.
  # create-client registers machine credentials' clients through client
  # registration, the one way the admin client creates a client.
  admin_client_id    = "identity-stack-admin"
  admin_client_roles = ["manage-users", "manage-organizations", "query-clients", "view-events", "create-client"]
  # Its fine-grained admin permission (v2) on the API client alone: read it,
  # manage its roles, and set their composites.
  admin_api_client_scopes = ["manage", "map-roles-composite", "view"]

  # How long Keycloak keeps user and admin events, in seconds: 7 days. The
  # backend's pull reads them; a reconciliation pass catches what expired, so
  # this outlasts the interval between passes.
  events_expiration_seconds = 604800

  # Sets $token to an Admin API token of the bootstrap admin the provider logs
  # in as (KEYCLOAK_USER, KEYCLOAK_PASSWORD from the environment). The password
  # and the token go to curl on stdin (printf is a shell builtin), never as an
  # argument another process could read from /proc/<pid>/cmdline.
  admin_token_command = <<-EOT
    token="$(printf '%s' "$KEYCLOAK_PASSWORD" | curl -fsS --max-time 20 "${local.keycloak_url}/realms/master/protocol/openid-connect/token" \
      --data-urlencode grant_type=password --data-urlencode client_id=admin-cli \
      --data-urlencode "username=$KEYCLOAK_USER" --data-urlencode password@- | jq -er .access_token)"
  EOT

  # Adds the user in $ASSIGNMENT to the organization's group, through the
  # Organization API.
  role_assignment_command = <<-EOT
    set -euo pipefail
    a() { jq -r --arg k "$1" '.[$k]' <<<"$ASSIGNMENT"; }
    ${local.admin_token_command}
    printf 'Authorization: Bearer %s\n' "$token" | curl -fsS --max-time 20 -o /dev/null -X PUT -H @- \
      "$(a keycloak_url)/admin/realms/$(a realm)/organizations/$(a organization_id)/groups/$(a group_id)/members/$(a user_id)"
  EOT
}

resource "keycloak_realm" "this" {
  realm                    = local.realm
  enabled                  = true
  ssl_required             = var.ssl_required
  registration_allowed     = false
  duplicate_emails_allowed = false
  # The backend links a Keycloak identity to an existing canonical user on a
  # verified email (docs/security.md), so the realm verifies emails.
  verify_email          = true
  organizations_enabled = true
  # Fine-grained admin permissions (v2): what scopes the admin service
  # account's client management to the API client.
  admin_permissions_enabled = true

  # Keycloak keeps admin events' expiry as a realm attribute; the events
  # resource below has no argument for it.
  attributes = {
    adminEventsExpiration = tostring(local.events_expiration_seconds)
    # Keycloak 26.8 allows a mapper type in client registration only when this
    # realm attribute names it too (the policy below names it as well): the
    # machine credentials' clients carry an audience mapper.
    allowedProtocolMapperTypesForClientAdministrators = "oidc-audience-mapper"
  }

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

# User events and admin events, admin events with their representations: what
# the backend's pull reads.
resource "keycloak_realm_events" "this" {
  realm_id                     = keycloak_realm.this.id
  events_enabled               = true
  events_expiration            = local.events_expiration_seconds
  admin_events_enabled         = true
  admin_events_details_enabled = true
  events_listeners             = ["jboss-logging"]
}

# Keycloak's default user profile, with the attributes it does not declare editable
# by admins only (ADMIN_EDIT): the backend's admin client keeps the profile's custom
# attributes there (KeycloakSyncAdapter, the custom_attributes capability), and a user
# can neither see nor set them in the account console.
resource "keycloak_realm_user_profile" "this" {
  realm_id                   = keycloak_realm.this.id
  unmanaged_attribute_policy = "ADMIN_EDIT"

  attribute {
    name         = "username"
    display_name = "$${username}"
    validator {
      name   = "length"
      config = { min = "3", max = "255" }
    }
    validator { name = "username-prohibited-characters" }
    validator { name = "up-username-not-idn-homograph" }
    permissions {
      view = ["admin", "user"]
      edit = ["admin", "user"]
    }
  }

  attribute {
    name               = "email"
    display_name       = "$${email}"
    required_for_roles = ["user"]
    validator { name = "email" }
    validator {
      name   = "length"
      config = { max = "255" }
    }
    permissions {
      view = ["admin", "user"]
      edit = ["admin", "user"]
    }
  }

  attribute {
    name               = "firstName"
    display_name       = "$${firstName}"
    required_for_roles = ["user"]
    validator {
      name   = "length"
      config = { max = "255" }
    }
    validator { name = "person-name-prohibited-characters" }
    permissions {
      view = ["admin", "user"]
      edit = ["admin", "user"]
    }
  }

  attribute {
    name               = "lastName"
    display_name       = "$${lastName}"
    required_for_roles = ["user"]
    validator {
      name   = "length"
      config = { max = "255" }
    }
    validator { name = "person-name-prohibited-characters" }
    permissions {
      view = ["admin", "user"]
      edit = ["admin", "user"]
    }
  }

  group {
    name                = "user-metadata"
    display_header      = "User metadata"
    display_description = "Attributes, which refer to user metadata"
  }
}

# --- The API client: audience, permissions and roles ---------------------------

resource "keycloak_openid_client" "api" {
  realm_id                     = keycloak_realm.this.id
  client_id                    = local.api_client_id
  name                         = "The identity-stack API: its id is the access token audience; its client roles are the canonical permissions and roles"
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

resource "keycloak_role" "permission" {
  for_each = local.permissions

  realm_id  = keycloak_realm.this.id
  client_id = keycloak_openid_client.api.id
  name      = each.key
}

resource "keycloak_role" "role" {
  for_each = local.roles

  realm_id        = keycloak_realm.this.id
  client_id       = keycloak_openid_client.api.id
  name            = each.key
  composite_roles = [for permission in each.value : keycloak_role.permission[permission].id]
}

# --- Public clients ----------------------------------------------------------------

resource "keycloak_openid_client" "public" {
  for_each = local.public_clients

  realm_id                        = keycloak_realm.this.id
  client_id                       = each.key
  name                            = each.value
  enabled                         = true
  access_type                     = "PUBLIC"
  standard_flow_enabled           = true
  implicit_flow_enabled           = false
  direct_access_grants_enabled    = false
  service_accounts_enabled        = false
  full_scope_allowed              = false
  pkce_code_challenge_method      = "S256"
  valid_redirect_uris             = [local.frontend_url]
  web_origins                     = [local.frontend_url]
  valid_post_logout_redirect_uris = ["${local.frontend_url}/login"]
}

# No `roles` scope: a token carries no resource_access or realm_access.
resource "keycloak_openid_client_default_scopes" "public" {
  for_each = local.public_clients

  realm_id       = keycloak_realm.this.id
  client_id      = keycloak_openid_client.public[each.key].id
  default_scopes = ["basic", "profile", "email", "web-origins", "acr"]
}

resource "keycloak_openid_client_optional_scopes" "public" {
  for_each = local.public_clients

  realm_id        = keycloak_realm.this.id
  client_id       = keycloak_openid_client.public[each.key].id
  optional_scopes = ["organization", "offline_access"]
}

# The SPA's access tokens, and only they, carry the API client's id in aud.
resource "keycloak_openid_audience_protocol_mapper" "spa_api_audience" {
  realm_id                   = keycloak_realm.this.id
  client_id                  = keycloak_openid_client.public[local.spa_client_id].id
  name                       = "API audience"
  included_client_audience   = keycloak_openid_client.api.client_id
  add_to_access_token        = true
  add_to_id_token            = false
  add_to_token_introspection = true
}

# --- Users -----------------------------------------------------------------------

# Login passwords for the fixture users, generated per apply and kept in local
# state only (output `test_credentials`). None is committed.
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

# Authoritative over each user's roles: none, as in the import (Keycloak gives
# a user created through the Admin API the realm's default role; the import
# does not).
resource "keycloak_user_roles" "this" {
  for_each = local.users

  realm_id   = keycloak_realm.this.id
  user_id    = keycloak_user.this[each.key].id
  role_ids   = []
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
}

# Authoritative: a member added to an organization by hand is removed on the
# next apply.
resource "keycloak_organization_memberships" "this" {
  for_each = local.organizations

  realm_id        = keycloak_realm.this.id
  organization_id = keycloak_organization.this[each.key].id
  members         = [for username in each.value.members : keycloak_user.this[username].username]
}

# --- Role assignments: organization groups -----------------------------------------

# One group per organization and canonical role, owned by the organization.
# No role mapping on any of them: membership records an assignment and grants
# nothing.
locals {
  # Where each role assignment lives: the ids the Organization API call names.
  role_assignment_targets = {
    for key, assignment in local.role_assignments : key => {
      keycloak_url    = local.keycloak_url
      realm           = keycloak_realm.this.realm
      organization_id = keycloak_organization.this[assignment.alias].id
      group_id        = keycloak_group.role["${assignment.alias}/${assignment.role}"].id
      user_id         = keycloak_user.this[assignment.username].id
    }
  }
}

resource "keycloak_group" "role" {
  for_each = local.role_groups

  realm_id        = keycloak_realm.this.id
  organization_id = keycloak_organization.this[each.value.alias].id
  name            = each.value.role
}

# Membership of an organization's group goes through the Organization API
# (PUT/DELETE /organizations/{id}/groups/{group}/members/{user}): Keycloak
# refuses the realm group API for an organization's group, and the provider
# has no resource for it. Each assignment is set here with the bootstrap admin
# the provider logs in as (KEYCLOAK_USER, KEYCLOAK_PASSWORD) and taken away on
# destroy. Keycloak refuses a user who is not a member of the organization.
resource "terraform_data" "role_assignment" {
  for_each = local.role_assignments

  # A changed id (a recreated user, group or organization) replaces the
  # resource, so the create provisioner runs again; an in-place update of
  # `input` would run no provisioner and lose the membership. `input` carries
  # the same ids to the destroy provisioner (self.input).
  triggers_replace = local.role_assignment_targets[each.key]
  input            = local.role_assignment_targets[each.key]

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = local.role_assignment_command
    environment = { ASSIGNMENT = jsonencode(self.input) }
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      a() { jq -r --arg k "$1" '.[$k]' <<<"$ASSIGNMENT"; }
      # The password and the token reach curl on stdin, as in local.admin_token_command.
      token="$(printf '%s' "$KEYCLOAK_PASSWORD" | curl -fsS --max-time 20 "$(a keycloak_url)/realms/master/protocol/openid-connect/token" \
        --data-urlencode grant_type=password --data-urlencode client_id=admin-cli \
        --data-urlencode "username=$KEYCLOAK_USER" --data-urlencode password@- | jq -er .access_token)"
      code="$(printf 'Authorization: Bearer %s\n' "$token" | curl -sS --max-time 20 -o /dev/null -w '%%{http_code}' -X DELETE -H @- \
        "$(a keycloak_url)/admin/realms/$(a realm)/organizations/$(a organization_id)/groups/$(a group_id)/members/$(a user_id)")"
      # 404: the realm, organization, group or membership is already gone.
      case "$code" in 204|404) ;; *) echo "removing the role assignment answered HTTP $code" >&2; exit 1 ;; esac
    EOT
    environment = { ASSIGNMENT = jsonencode(self.input) }
  }

  depends_on = [keycloak_organization_memberships.this]
}

# --- Client registration: machine credentials' clients --------------------------------

# The backend registers each machine credential's client with an audience mapper
# naming the API client (identity-stack-api), so its client-credentials tokens
# carry the API audience. Keycloak's authenticated "Allowed Protocol Mapper
# Types" client registration policy refuses that mapper type by default; this
# adds it to that one policy, as the import does. The provider has no resource
# for client registration policies, so the Admin API sets it.
resource "terraform_data" "registration_audience_mapper" {
  input = {
    keycloak_url      = local.keycloak_url
    realm             = keycloak_realm.this.realm
    realm_internal_id = keycloak_realm.this.internal_id
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -euo pipefail
      a() { jq -r --arg k "$1" '.[$k]' <<<"$POLICY"; }
      ${local.admin_token_command}
      base="$(a keycloak_url)/admin/realms/$(a realm)"
      policy="$(printf 'Authorization: Bearer %s\n' "$token" | curl -fsS --max-time 20 -H @- \
        "$base/components?type=org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy" |
        jq -ce '[.[] | select(.subType == "authenticated" and .providerId == "allowed-protocol-mappers")] | if length == 1 then .[0] else error("expected one authenticated mapper policy") end')"
      updated="$(jq -c '.config["allowed-protocol-mapper-types"] |= ((. // []) + ["oidc-audience-mapper"] | unique)' <<<"$policy")"
      printf 'Authorization: Bearer %s\n' "$token" | curl -fsS --max-time 20 -o /dev/null -X PUT -H @- \
        -H 'Content-Type: application/json' --data-binary @<(printf '%s' "$updated") \
        "$base/components/$(jq -r .id <<<"$policy")"
    EOT
    environment = { POLICY = jsonencode(self.input) }
  }
}

# --- Scoped admin permission on the API client ---------------------------------------

# The id of the admin-permissions client, the resource server of every
# fine-grained admin permission. The provider's keycloak_openid_client data
# source cannot read it: it also reads the client's secret, which Keycloak
# 26.8 refuses even to the bootstrap admin (403). The realm's own
# representation names the client (adminPermissionsClient).
data "external" "admin_permissions_client" {
  program = ["bash", "-c", <<-EOT
    set -euo pipefail
    query="$(cat)"
    q() { jq -r --arg k "$1" '.[$k]' <<<"$query"; }
    ${local.admin_token_command}
    printf 'Authorization: Bearer %s\n' "$token" | curl -fsS --max-time 20 -H @- "$(q keycloak_url)/admin/realms/$(q realm)" |
      jq -ce '{id: .adminPermissionsClient.id}'
  EOT
  ]
  query = {
    keycloak_url = local.keycloak_url
    # The realm's internal id, unknown until the realm exists: the lookup waits for it.
    realm             = keycloak_realm.this.realm
    realm_internal_id = keycloak_realm.this.internal_id
  }
}

resource "keycloak_openid_client_user_policy" "admin" {
  realm_id           = keycloak_realm.this.id
  resource_server_id = data.external.admin_permissions_client.result.id
  name               = "${local.admin_client_id} service account"
  users              = [keycloak_openid_client.admin.service_account_user_id]
  logic              = "POSITIVE"
  decision_strategy  = "UNANIMOUS"
}

# The admin service account may read the API client and manage its roles and
# their composites, and no other client (fine-grained admin permissions v2).
resource "keycloak_openid_client_admin_permissions" "admin_api_client" {
  realm_id          = keycloak_realm.this.id
  name              = "${local.admin_client_id} manages the API client"
  client_ids        = [keycloak_openid_client.api.id]
  scopes            = local.admin_api_client_scopes
  policies          = [keycloak_openid_client_user_policy.admin.id]
  decision_strategy = "UNANIMOUS"
}

# And may read the realm-management client: without it Keycloak leaves that
# client out of the service account's own role mappings, which the adapter
# reads at startup to confirm its roles. Changing that client, or its own
# roles, stays refused.
resource "keycloak_openid_client_admin_permissions" "admin_realm_management" {
  realm_id          = keycloak_realm.this.id
  name              = "${local.admin_client_id} reads the realm-management client"
  client_ids        = [data.keycloak_openid_client.realm_management.id]
  scopes            = ["view"]
  policies          = [keycloak_openid_client_user_policy.admin.id]
  decision_strategy = "UNANIMOUS"
}

# --- Admin service account ---------------------------------------------------------

data "keycloak_openid_client" "realm_management" {
  realm_id  = keycloak_realm.this.id
  client_id = "realm-management"
}

data "keycloak_role" "realm_management" {
  for_each = toset(local.admin_client_roles)

  realm_id  = keycloak_realm.this.id
  client_id = data.keycloak_openid_client.realm_management.id
  name      = each.key
}

resource "keycloak_openid_client" "admin" {
  realm_id                     = keycloak_realm.this.id
  client_id                    = local.admin_client_id
  name                         = "Backend service account: pushes to and pulls from the realm"
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
resource "keycloak_openid_client_default_scopes" "admin" {
  realm_id       = keycloak_realm.this.id
  client_id      = keycloak_openid_client.admin.id
  default_scopes = ["kc-admin-api"]
}

resource "keycloak_openid_client_optional_scopes" "admin" {
  realm_id        = keycloak_realm.this.id
  client_id       = keycloak_openid_client.admin.id
  optional_scopes = []
}

# Authoritative over the service account's roles: exactly these
# realm-management roles and no realm role, as in the import (Keycloak gives a
# new service account the realm's default role, as it does a user).
resource "keycloak_user_roles" "admin" {
  realm_id   = keycloak_realm.this.id
  user_id    = keycloak_openid_client.admin.service_account_user_id
  role_ids   = [for role in local.admin_client_roles : data.keycloak_role.realm_management[role].id]
  exhaustive = true
}

resource "keycloak_generic_role_mapper" "admin" {
  for_each = toset(local.admin_client_roles)

  realm_id  = keycloak_realm.this.id
  client_id = keycloak_openid_client.admin.id
  role_id   = data.keycloak_role.realm_management[each.key].id
}
