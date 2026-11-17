# Everything inside the local Zitadel instance that identity-stack relies on.
#
# Model: a Zitadel organization is a tenant's identity home. The canonical
# store (backend database) decides access: the seed (`make zitadel-seed`) links
# the tenant organizations to canonical tenants by id. Nothing here grants a
# Zitadel role to a user, and the project does not check roles, so no Zitadel
# role decides anything.

locals {
  # The project's role definitions. They mirror the canonical role names and
  # are granted to nobody.
  project_roles = {
    owner  = "Owner"
    admin  = "Admin"
    member = "Member"
    viewer = "Viewer"
  }

  # The test tenants and users (infra/fixtures/test-identities.json).
  fixture = jsondecode(file("${path.module}/../fixtures/test-identities.json"))

  # Tenant organizations, each granted the project: the fixture's tenants a
  # canonical tenant names. The seed names a canonical tenant after each key.
  tenant_organizations = { for key, tenant in local.fixture.tenants : key => tenant.name if tenant.canonical }

  # Every organization a test user lives in: every fixture tenant, those no
  # canonical tenant names included.
  user_organizations = { for key, tenant in local.fixture.tenants : key => tenant.name }

  # One human test user per fixture user, in the organization of their home
  # tenant, email verified.
  test_users = {
    for key, user in local.fixture.users : key => {
      organization = user.home
      user_name    = key
      email        = "${key}@${local.fixture.tenants[user.home].domain}"
      first_name   = user.first_name
      last_name    = user.last_name
    }
  }

  # The user `make zitadel-seed` links to canonical tenants beforehand: the
  # fixture's user in two tenants, with their canonical role in each tenant a
  # canonical tenant names. Every other user's first login provisions them.
  linked_user = local.fixture.cases.two_tenants
  linked_roles = {
    for tenant, role in local.fixture.users[local.linked_user].roles : tenant => role
    if role != null && contains(keys(local.tenant_organizations), tenant)
  }

  # Zitadel's check lifetimes on a fresh instance, by length.
  ten_days    = "240h0m0s"
  thirty_days = "720h0m0s"

  # Login policy for the instance default and every organization. Only
  # allow_register departs from the values a fresh instance has: nobody may
  # place themselves in an organization, because the organization decides the
  # tenant a user is provisioned into.
  login_policy = {
    user_login                    = true
    allow_register                = false
    allow_external_idp            = false
    force_mfa                     = false
    force_mfa_local_only          = false
    passwordless_type             = "PASSWORDLESS_TYPE_ALLOWED"
    hide_password_reset           = false
    ignore_unknown_usernames      = false
    default_redirect_uri          = ""
    allow_domain_discovery        = true
    password_check_lifetime       = local.ten_days
    external_login_check_lifetime = local.ten_days
    mfa_init_skip_lifetime        = local.thirty_days
    second_factor_check_lifetime  = "18h0m0s"
    multi_factor_check_lifetime   = "12h0m0s"
    second_factors                = ["SECOND_FACTOR_TYPE_OTP", "SECOND_FACTOR_TYPE_U2F"]
    multi_factors                 = ["MULTI_FACTOR_TYPE_U2F_WITH_VERIFICATION"]
  }

  redirect_uris             = [var.frontend_url]
  post_logout_redirect_uris = ["${var.frontend_url}/login"]
}

# --- Instance settings --------------------------------------------------------

resource "zitadel_default_login_policy" "this" {
  user_login                    = local.login_policy.user_login
  allow_register                = local.login_policy.allow_register
  allow_external_idp            = local.login_policy.allow_external_idp
  force_mfa                     = local.login_policy.force_mfa
  force_mfa_local_only          = local.login_policy.force_mfa_local_only
  passwordless_type             = local.login_policy.passwordless_type
  hide_password_reset           = local.login_policy.hide_password_reset
  ignore_unknown_usernames      = local.login_policy.ignore_unknown_usernames
  default_redirect_uri          = local.login_policy.default_redirect_uri
  allow_domain_discovery        = local.login_policy.allow_domain_discovery
  password_check_lifetime       = local.login_policy.password_check_lifetime
  external_login_check_lifetime = local.login_policy.external_login_check_lifetime
  mfa_init_skip_lifetime        = local.login_policy.mfa_init_skip_lifetime
  second_factor_check_lifetime  = local.login_policy.second_factor_check_lifetime
  multi_factor_check_lifetime   = local.login_policy.multi_factor_check_lifetime
  second_factors                = local.login_policy.second_factors
  multi_factors                 = local.login_policy.multi_factors
}

resource "zitadel_default_oidc_settings" "this" {
  access_token_lifetime = var.access_token_lifetime
  # The rest are a fresh instance's values, stated because the resource
  # requires them.
  id_token_lifetime             = "12h0m0s"
  refresh_token_idle_expiration = "720h0m0s"
  refresh_token_expiration      = "2160h0m0s"
}

# --- Organizations ------------------------------------------------------------

resource "zitadel_org" "platform" {
  name = "identity-stack"
}

resource "zitadel_org" "this" {
  for_each = local.user_organizations

  name = each.value
}

resource "zitadel_login_policy" "platform" {
  org_id = zitadel_org.platform.id

  user_login                    = local.login_policy.user_login
  allow_register                = local.login_policy.allow_register
  allow_external_idp            = local.login_policy.allow_external_idp
  force_mfa                     = local.login_policy.force_mfa
  force_mfa_local_only          = local.login_policy.force_mfa_local_only
  passwordless_type             = local.login_policy.passwordless_type
  hide_password_reset           = local.login_policy.hide_password_reset
  ignore_unknown_usernames      = local.login_policy.ignore_unknown_usernames
  default_redirect_uri          = local.login_policy.default_redirect_uri
  allow_domain_discovery        = local.login_policy.allow_domain_discovery
  password_check_lifetime       = local.login_policy.password_check_lifetime
  external_login_check_lifetime = local.login_policy.external_login_check_lifetime
  mfa_init_skip_lifetime        = local.login_policy.mfa_init_skip_lifetime
  second_factor_check_lifetime  = local.login_policy.second_factor_check_lifetime
  multi_factor_check_lifetime   = local.login_policy.multi_factor_check_lifetime
  second_factors                = local.login_policy.second_factors
  multi_factors                 = local.login_policy.multi_factors
}

resource "zitadel_login_policy" "this" {
  for_each = zitadel_org.this

  org_id = each.value.id

  user_login                    = local.login_policy.user_login
  allow_register                = local.login_policy.allow_register
  allow_external_idp            = local.login_policy.allow_external_idp
  force_mfa                     = local.login_policy.force_mfa
  force_mfa_local_only          = local.login_policy.force_mfa_local_only
  passwordless_type             = local.login_policy.passwordless_type
  hide_password_reset           = local.login_policy.hide_password_reset
  ignore_unknown_usernames      = local.login_policy.ignore_unknown_usernames
  default_redirect_uri          = local.login_policy.default_redirect_uri
  allow_domain_discovery        = local.login_policy.allow_domain_discovery
  password_check_lifetime       = local.login_policy.password_check_lifetime
  external_login_check_lifetime = local.login_policy.external_login_check_lifetime
  mfa_init_skip_lifetime        = local.login_policy.mfa_init_skip_lifetime
  second_factor_check_lifetime  = local.login_policy.second_factor_check_lifetime
  multi_factor_check_lifetime   = local.login_policy.multi_factor_check_lifetime
  second_factors                = local.login_policy.second_factors
  multi_factors                 = local.login_policy.multi_factors
}

# --- Project, roles and grants ------------------------------------------------

resource "zitadel_project" "this" {
  org_id = zitadel_org.platform.id
  name   = "identity-stack"

  # No role decides access in Zitadel: authentication does not require a role
  # grant, nor a project grant to the user's organization, and tokens carry no
  # roles claim.
  project_role_check     = false
  has_project_check      = false
  project_role_assertion = false
}

resource "zitadel_project_role" "this" {
  for_each = local.project_roles

  org_id       = zitadel_org.platform.id
  project_id   = zitadel_project.this.id
  role_key     = each.key
  display_name = each.value
}

resource "zitadel_project_grant" "this" {
  for_each = local.tenant_organizations

  org_id         = zitadel_org.platform.id
  project_id     = zitadel_project.this.id
  granted_org_id = zitadel_org.this[each.key].id
  role_keys      = sort(keys(zitadel_project_role.this))
}

# --- Applications -------------------------------------------------------------

# The SPA: a public client with authorization code and PKCE, JWT access tokens
# without a roles claim, and refresh tokens.
resource "zitadel_application_oidc" "spa" {
  org_id     = zitadel_org.platform.id
  project_id = zitadel_project.this.id
  name       = "identity-stack-spa"

  app_type                    = "OIDC_APP_TYPE_USER_AGENT"
  auth_method_type            = "OIDC_AUTH_METHOD_TYPE_NONE"
  grant_types                 = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE", "OIDC_GRANT_TYPE_REFRESH_TOKEN"]
  response_types              = ["OIDC_RESPONSE_TYPE_CODE"]
  access_token_type           = "OIDC_TOKEN_TYPE_JWT"
  access_token_role_assertion = false
  id_token_role_assertion     = false
  id_token_userinfo_assertion = false
  redirect_uris               = local.redirect_uris
  post_logout_redirect_uris   = local.post_logout_redirect_uris
  # Plain-http redirect URIs need development mode.
  dev_mode = startswith(var.frontend_url, "http://")
}

# A second application in the same project, for negative tests. A human's
# access token lists every application of the project in `aud`, so a token
# issued to this application proves that audience alone does not identify the
# SPA.
resource "zitadel_application_oidc" "other" {
  org_id     = zitadel_org.platform.id
  project_id = zitadel_project.this.id
  name       = "identity-stack-other"

  app_type                    = "OIDC_APP_TYPE_USER_AGENT"
  auth_method_type            = "OIDC_AUTH_METHOD_TYPE_NONE"
  grant_types                 = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  response_types              = ["OIDC_RESPONSE_TYPE_CODE"]
  access_token_type           = "OIDC_TOKEN_TYPE_JWT"
  access_token_role_assertion = false
  id_token_role_assertion     = false
  id_token_userinfo_assertion = false
  redirect_uris               = local.redirect_uris
  post_logout_redirect_uris   = local.post_logout_redirect_uris
  dev_mode                    = startswith(var.frontend_url, "http://")
}

# --- Users --------------------------------------------------------------------

# Machine user for tests: client credentials, JWT access tokens. It holds no
# role and no membership.
resource "zitadel_machine_user" "tests" {
  org_id            = zitadel_org.platform.id
  user_name         = "identity-stack-tests"
  name              = "identity-stack tests"
  access_token_type = "ACCESS_TOKEN_TYPE_JWT"
  with_secret       = true
}

# Machine user the backend's sync adapter calls Zitadel as (SYNC_PROVIDER=zitadel):
# it creates tenant organizations, their login policies and project grants, and
# human users in them. IAM_ORG_MANAGER is the narrowest instance role that may
# create an organization; IAM_USER_MANAGER may not.
resource "zitadel_machine_user" "sync" {
  org_id            = zitadel_org.platform.id
  user_name         = "identity-stack-sync"
  name              = "identity-stack sync adapter"
  access_token_type = "ACCESS_TOKEN_TYPE_BEARER"
}

resource "zitadel_instance_member" "sync" {
  user_id = zitadel_machine_user.sync.id
  roles   = ["IAM_ORG_MANAGER"]
}

# The adapter's credential (ZITADEL_ADMIN_TOKEN). The instance is disposable, so
# the token lives as long as it does.
resource "zitadel_personal_access_token" "sync" {
  org_id          = zitadel_org.platform.id
  user_id         = zitadel_machine_user.sync.id
  expiration_date = "2099-01-01T00:00:00Z"
}

resource "random_password" "user" {
  for_each = local.test_users

  length      = 24
  min_lower   = 1
  min_upper   = 1
  min_numeric = 1
  min_special = 1
  # Characters that need no quoting in a shell or a URL.
  override_special = "-_.!"
}

resource "zitadel_human_user" "this" {
  for_each = local.test_users

  org_id                       = zitadel_org.this[each.value.organization].id
  user_name                    = each.value.user_name
  first_name                   = each.value.first_name
  last_name                    = each.value.last_name
  email                        = each.value.email
  is_email_verified            = true
  initial_password             = random_password.user[each.key].result
  initial_skip_password_change = true
}
