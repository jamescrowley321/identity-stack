"""The Credentials context: what a new machine credential may be, and whether one is in force."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from expression import Error, Ok

from app.domain import credentials
from app.errors.credentials import identity_error
from app.errors.identity import GrantRefused, NotFound, ValidationError

NOW = 1_800_000_000
MOMENT = datetime(2027, 1, 15, 12, 0, tzinfo=timezone.utc)


def _credential(**changes) -> credentials.MachineCredential:
    values = {
        "id": uuid.UUID(int=1),
        "tenant_id": uuid.UUID(int=2),
        "provider": "keycloak",
        "name": "robot",
        "client_id": "identity-stack-machine-1",
        "subject": "sub-1",
        "external_id": "client-1",
        "status": credentials.CredentialStatus.ACTIVE,
        "expires_at": MOMENT + timedelta(days=1),
        "created_at": MOMENT,
        "role_names": ("admin",),
    }
    return credentials.MachineCredential(**{**values, **changes})


class TestExpiry:
    @pytest.mark.parametrize(
        ("expire_time", "expected"),
        [
            (None, Error(credentials.ExpiryMissing())),
            (0, Error(credentials.ExpiryMissing())),
            (NOW, Error(credentials.ExpiryNotInFuture())),
            (NOW - 1, Error(credentials.ExpiryNotInFuture())),
            (NOW + credentials.MAX_LIFETIME_SECONDS + 1, Error(credentials.ExpiryTooFar(max_days=365))),
            (NOW + 1, Ok(NOW + 1)),
            (NOW + credentials.MAX_LIFETIME_SECONDS, Ok(NOW + credentials.MAX_LIFETIME_SECONDS)),
        ],
        ids=["absent", "zero", "now", "past", "too far", "next second", "the longest"],
    )
    def test_a_bounded_future_expiry_only(self, expire_time, expected):
        assert credentials.expiry(expire_time, now=NOW) == expected


class TestRoles:
    def test_roles_the_creator_holds_are_granted_once_each(self):
        assert credentials.grantable_roles(("admin", "admin", "member"), held=frozenset({"admin", "member"})) == Ok(
            ("admin", "member")
        )

    def test_roles_the_creator_lacks_are_named(self):
        assert credentials.grantable_roles(("owner", "admin", "billing"), held=frozenset({"admin"})) == Error(
            credentials.RolesNotHeld(roles=("billing", "owner"))
        )

    def test_roles_the_tenant_lacks_are_named(self):
        assert credentials.known_roles(("admin", "ghost"), found=frozenset({"admin"})) == Error(
            credentials.RolesNotFound(roles=("ghost",))
        )

    def test_known_roles_pass(self):
        assert credentials.known_roles(("admin",), found=frozenset({"admin"})) == Ok(("admin",))


class TestInForce:
    @pytest.mark.parametrize(
        ("credential", "expected"),
        [
            (_credential(), True),
            (_credential(status=credentials.CredentialStatus.INACTIVE), False),
            (_credential(expires_at=MOMENT), False),
            (_credential(expires_at=MOMENT - timedelta(seconds=1)), False),
        ],
        ids=["active", "inactive", "expiring now", "expired"],
    )
    def test_active_and_unexpired_only(self, credential, expected):
        assert credentials.in_force(credential, now=MOMENT) is expected


TRANSLATIONS = [
    (
        credentials.ExpiryMissing(),
        ValidationError(
            message=(
                "expire_time is required and must be a future epoch-seconds timestamp; "
                "a key that never expires outlives the membership that created it"
            )
        ),
    ),
    (credentials.ExpiryNotInFuture(), ValidationError(message="expire_time must be in the future")),
    (
        credentials.ExpiryTooFar(max_days=365),
        ValidationError(message="expire_time may be at most 365 days in the future"),
    ),
    (
        credentials.RolesNotHeld(roles=("billing", "owner")),
        GrantRefused(message="Cannot grant roles you do not hold: billing, owner"),
    ),
    (credentials.RolesNotFound(roles=("ghost",)), ValidationError(message="The tenant has no roles named: ghost")),
    (credentials.CredentialNotFound(credential_id="x"), NotFound(message="Machine credential not found")),
]


@pytest.mark.parametrize(("error", "expected"), TRANSLATIONS)
def test_each_error_has_its_identity_error(error, expected):
    assert identity_error(error) == expected


def test_every_credential_error_is_translated():
    assert {type(error) for error, _ in TRANSLATIONS} == set(credentials.CredentialError.__subclasses__())


def test_an_unknown_credential_error_is_refused():
    with pytest.raises(TypeError):
        identity_error(credentials.CredentialError())
