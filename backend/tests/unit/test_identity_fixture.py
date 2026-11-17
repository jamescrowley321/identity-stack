"""The shared test fixture (``infra/fixtures/test-identities.json``) and the Keycloak import that mirrors it.

The provider Terraform roots build their tenants, users and memberships from the
fixture. The Keycloak realm import is a file of its own, so this pins it to the
fixture: the same users, organizations and members, and the admin record
(``type:tenant-admin``) on exactly the fixture's admins.
"""

import copy
import json

import pytest

from tests.helpers.identity_fixture import (
    FIXTURE_PATH,
    IDENTITIES,
    IdentityFixtureInvalid,
    load_identity_fixture,
)
from tests.helpers.keycloak_target import REALM_IMPORT_PATH, import_users

RAW = json.loads(FIXTURE_PATH.read_text())
REALM = json.loads(REALM_IMPORT_PATH.read_text())
# The organization-bound type the Keycloak realm records an admin under (infra/keycloak main.tf).
ADMIN_TYPE_ATTRIBUTE = "type:tenant-admin"


def _members(tenant: str) -> set[str]:
    return {key for key, user in IDENTITIES.users.items() if tenant in user.roles}


def _with(path: tuple, value) -> dict:
    raw = copy.deepcopy(RAW)
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return raw


# --- The cases the suites stand on ------------------------------------------------


def test_the_two_tenant_user_is_a_member_of_two_tenants_with_a_different_role_in_each():
    roles = IDENTITIES.cases.two_tenants.roles

    assert len(roles) == 2
    assert len(set(roles.values())) == 2


def test_the_no_grant_user_holds_no_role_anywhere():
    assert set(IDENTITIES.cases.no_grant.roles.values()) == {None}


def test_the_tenant_admin_is_a_member_of_one_tenant_and_admin_there():
    user = IDENTITIES.cases.tenant_admin

    assert user.roles == {user.home: "admin"}


def test_the_member_is_a_member_of_one_tenant_with_role_member():
    user = IDENTITIES.cases.member

    assert user.roles == {user.home: "member"}


def test_the_unlinked_tenant_user_is_only_in_tenants_no_canonical_tenant_names():
    user = IDENTITIES.cases.unlinked_tenant

    assert user.roles
    assert {IDENTITIES.tenants[tenant].canonical for tenant in user.roles} == {False}


def test_every_user_email_is_in_their_home_tenant_domain():
    for key, user in IDENTITIES.users.items():
        assert user.email == f"{key}@{IDENTITIES.tenants[user.home].domain}"


# --- The Keycloak realm import mirrors the fixture --------------------------------


def test_the_realm_import_has_the_fixture_users_with_their_names_and_emails():
    imported = {
        user["username"]: (user["email"], user["firstName"], user["lastName"], user["emailVerified"])
        for user in REALM["users"]
        if user["username"] in import_users(REALM)
    }

    assert imported == {
        key: (user.email, user.first_name, user.last_name, True) for key, user in IDENTITIES.users.items()
    }


def test_the_realm_import_has_one_organization_per_fixture_tenant():
    imported = {
        organization["alias"]: (organization["name"], [domain["name"] for domain in organization["domains"]])
        for organization in REALM["organizations"]
    }

    assert imported == {key: (tenant.name, [tenant.domain]) for key, tenant in IDENTITIES.tenants.items()}


def test_the_realm_import_organizations_have_the_fixture_members():
    imported = {
        organization["alias"]: {member["username"] for member in organization.get("members", [])}
        for organization in REALM["organizations"]
    }

    assert imported == {tenant: _members(tenant) for tenant in IDENTITIES.tenants}


def test_the_realm_import_records_the_fixture_admins_of_each_organization():
    ids = {username: user.id for username, user in import_users(REALM).items()}
    imported = {
        organization["alias"]: set(organization.get("attributes", {}).get(ADMIN_TYPE_ATTRIBUTE, []))
        for organization in REALM["organizations"]
    }

    assert imported == {
        tenant: {ids[key] for key, user in IDENTITIES.users.items() if user.roles.get(tenant) == "admin"}
        for tenant in IDENTITIES.tenants
    }


# --- A fixture that contradicts itself is refused ---------------------------------


def test_the_fixture_file_loads():
    assert load_identity_fixture(RAW) == IDENTITIES


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("users", "alice", "home"), "initech", "home 'initech' is no fixture tenant"),
        (("users", "alice", "roles"), {"globex": "member"}, "member of their home tenant 'acme'"),
        (("users", "alice", "roles"), {"acme": "member", "initech": None}, "'initech', which is no fixture tenant"),
        (("users", "alice", "roles", "acme"), "superuser", "'superuser', not a canonical role"),
        (("cases", "member"), "zed", r"cases \['member'\] name no fixture user"),
        (("tenants", "acme"), {"name": "Acme"}, "not the expected shape"),
    ],
)
def test_a_contradictory_fixture_is_refused(path, value, message):
    with pytest.raises(IdentityFixtureInvalid, match=message):
        load_identity_fixture(_with(path, value))


def test_a_fixture_missing_a_case_is_refused():
    raw = copy.deepcopy(RAW)
    del raw["cases"]["no_grant"]

    with pytest.raises(IdentityFixtureInvalid, match="cases must name exactly"):
        load_identity_fixture(raw)


def test_a_fixture_that_is_not_an_object_is_refused():
    with pytest.raises(IdentityFixtureInvalid, match="must be a JSON object"):
        load_identity_fixture([])
