"""The shared test tenants and users: ``infra/fixtures/test-identities.json``.

The provider Terraform roots that seed a provider (``infra/keycloak``,
``infra/zitadel``) build their tenants, users and memberships from that file,
and the Keycloak realm import mirrors it. Tests take a fixture user's name from
here (``IDENTITIES.cases``), or from a root's outputs, never from a literal.

Loading refuses a fixture that contradicts itself (``IdentityFixtureInvalid``),
so a bad edit fails every test that reads it, at collection.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

FIXTURE_PATH = Path(__file__).resolve().parents[3] / "infra" / "fixtures" / "test-identities.json"
# The canonical roles a fixture user can hold in a tenant; None is a member with no grant.
CANONICAL_ROLES = frozenset({"owner", "admin", "member", "viewer"})
# Keys a root already uses beside the fixture's tenants: infra/zitadel's organization_ids
# output names its project-owning organization ``platform``.
RESERVED_TENANT_KEYS = frozenset({"platform"})


class IdentityFixtureInvalid(ValueError):
    """``test-identities.json`` names something it does not define, or is not the expected shape."""


@dataclass(frozen=True)
class FixtureTenant:
    key: str
    name: str
    domain: str
    # A canonical tenant names this tenant (Zitadel: the seed links its organization).
    canonical: bool


@dataclass(frozen=True)
class FixtureUser:
    key: str
    first_name: str
    last_name: str
    # The tenant the user lives in: their email's domain, and their Zitadel organization.
    home: str
    email: str
    # Each tenant the user is a member of, to their canonical role there (None: no grant).
    roles: Mapping[str, str | None]


@dataclass(frozen=True)
class FixtureCases:
    """The user each case of the suites stands on."""

    # A member of one tenant.
    member: FixtureUser
    # A member of two tenants, with a different role in each.
    two_tenants: FixtureUser
    # A member of a tenant with no grant there.
    no_grant: FixtureUser
    # An admin of exactly one tenant.
    tenant_admin: FixtureUser
    # A member whose Keycloak client roles give documents:write alone.
    write_only: FixtureUser
    # An admin of one tenant whom tests move to another tenant and back.
    moving_admin: FixtureUser
    # The member of a tenant no canonical tenant names.
    unlinked_tenant: FixtureUser


@dataclass(frozen=True)
class IdentityFixture:
    tenants: Mapping[str, FixtureTenant]
    users: Mapping[str, FixtureUser]
    cases: FixtureCases


def load_identity_fixture(raw: object) -> IdentityFixture:
    """Read the parsed fixture, refusing anything it names but does not define."""
    if not isinstance(raw, dict):
        raise IdentityFixtureInvalid("the fixture must be a JSON object")
    try:
        tenants = {key: _tenant(key, spec) for key, spec in raw["tenants"].items()}
        users = {key: _user(key, spec, tenants) for key, spec in raw["users"].items()}
        named = raw["cases"]
    except (KeyError, TypeError, AttributeError) as exc:
        raise IdentityFixtureInvalid(f"the fixture is not the expected shape ({exc!r})") from None

    case_names = {field.name for field in fields(FixtureCases)}
    if not isinstance(named, dict) or set(named) != case_names:
        raise IdentityFixtureInvalid(f"cases must name exactly {sorted(case_names)}")
    unknown = sorted(case for case, user in named.items() if not isinstance(user, str) or user not in users)
    if unknown:
        raise IdentityFixtureInvalid(f"cases {unknown} name no fixture user")
    return IdentityFixture(tenants, users, FixtureCases(**{case: users[user] for case, user in named.items()}))


def _typed(owner: str, spec: dict, field: str, kind: type) -> object:
    # Terraform and Python read a mistyped value differently ("false" is false to one, true
    # to the other), so the tests would check a different set than the roots build.
    value = spec[field]
    if not isinstance(value, kind):
        raise IdentityFixtureInvalid(f"{owner}'s {field} is {value!r}, not a {_KIND_NAMES[kind]}")
    return value


_KIND_NAMES = {bool: "boolean", str: "string"}


def _tenant(key: str, spec: dict) -> FixtureTenant:
    if key in RESERVED_TENANT_KEYS:
        raise IdentityFixtureInvalid(f"tenant key {key!r} is reserved: a root already uses it beside the tenants")
    owner = f"tenant {key}"
    return FixtureTenant(
        key,
        _typed(owner, spec, "name", str),
        _typed(owner, spec, "domain", str),
        _typed(owner, spec, "canonical", bool),
    )


def _user(key: str, spec: dict, tenants: Mapping[str, FixtureTenant]) -> FixtureUser:
    owner = f"user {key}"
    first_name, last_name = _typed(owner, spec, "first_name", str), _typed(owner, spec, "last_name", str)
    home, roles = spec["home"], spec["roles"]
    if home not in tenants:
        raise IdentityFixtureInvalid(f"user {key}'s home {home!r} is no fixture tenant")
    if not isinstance(roles, dict) or home not in roles:
        raise IdentityFixtureInvalid(f"user {key} must be a member of their home tenant {home!r}")
    for tenant, role in roles.items():
        if tenant not in tenants:
            raise IdentityFixtureInvalid(f"user {key} is a member of {tenant!r}, which is no fixture tenant")
        if role is not None and role not in CANONICAL_ROLES:
            raise IdentityFixtureInvalid(f"user {key}'s role in {tenant!r} is {role!r}, not a canonical role")
    return FixtureUser(
        key=key,
        first_name=first_name,
        last_name=last_name,
        home=home,
        email=f"{key}@{tenants[home].domain}",
        roles=dict(roles),
    )


IDENTITIES = load_identity_fixture(json.loads(FIXTURE_PATH.read_text()))
