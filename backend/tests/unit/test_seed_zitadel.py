"""Reading the Terraform output the Zitadel seed runs from (`canonical_seed` in infra/zitadel)."""

import copy
import json
from pathlib import Path

import pytest

from app.models.identity.provider import ProviderType
from scripts.seed_zitadel import (
    PROVIDER_NAME,
    LinkedUser,
    SeedInput,
    SeedInputError,
    parse_seed_input,
    zitadel_provider,
)

ISSUER = "http://zitadel.localhost:8480"
ACME_ORG = "399943487992119299"
GLOBEX_ORG = "399943487958564867"
ACME_USER = "399943491398025219"

OUTPUT = {
    "issuer_url": ISSUER,
    "tenants": {"acme": ACME_ORG, "globex": GLOBEX_ORG},
    "linked_user": {
        "external_sub": ACME_USER,
        "email": "acme-user@acme.test",
        "user_name": "acme-user",
        "given_name": "Acme",
        "family_name": "User",
        "roles": {"acme": "admin", "globex": "viewer"},
    },
}


def _output_with(path: tuple[str, ...], value: object) -> dict:
    output = copy.deepcopy(OUTPUT)
    target = output
    for key in path[:-1]:
        target = target[key]
    if value is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return output


_DELETE = object()

# `terraform output -json canonical_seed` from an apply of infra/zitadel to the compose
# Zitadel. Re-record it when outputs.tf changes the output's shape.
RECORDED_OUTPUT = Path(__file__).resolve().parents[1] / "fixtures" / "zitadel" / "canonical-seed.json"


def test_the_terraform_output_reads_as_the_seed_input():
    assert parse_seed_input(OUTPUT) == SeedInput(
        issuer_url=ISSUER,
        tenants={"acme": ACME_ORG, "globex": GLOBEX_ORG},
        linked_user=LinkedUser(
            external_sub=ACME_USER,
            email="acme-user@acme.test",
            user_name="acme-user",
            given_name="Acme",
            family_name="User",
            roles={"acme": "admin", "globex": "viewer"},
        ),
    )


def test_the_output_a_real_apply_writes_reads_as_the_seed_input():
    recorded = json.loads(RECORDED_OUTPUT.read_text())

    assert parse_seed_input(recorded) == SeedInput(
        issuer_url=ISSUER,
        tenants={"acme": "399948686462824451", "globex": "399948686446047235"},
        linked_user=LinkedUser(
            external_sub="399948689885442051",
            email="acme-user@acme.test",
            user_name="acme-user",
            given_name="Acme",
            family_name="User",
            roles={"acme": "admin", "globex": "viewer"},
        ),
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("issuer_url",), _DELETE),
        (("issuer_url",), ""),
        (("tenants",), {}),
        (("tenants",), ["acme", "globex"]),
        (("tenants", "acme"), ""),
        (("tenants", "acme"), 399943487992119299),
        (("linked_user",), _DELETE),
        (("linked_user", "external_sub"), ""),
        (("linked_user", "email"), _DELETE),
        (("linked_user", "roles"), {}),
        (("linked_user", "roles", "initech"), "viewer"),
        (("linked_user", "roles", "acme"), "superuser"),
    ],
    ids=[
        "no issuer",
        "empty issuer",
        "no tenants",
        "tenants not a map",
        "empty organization id",
        "organization id not a string",
        "no linked user",
        "empty sub",
        "no email",
        "no roles",
        "role in a tenant the output does not name",
        "role that is not canonical",
    ],
)
def test_an_incomplete_or_inconsistent_output_is_refused(path, value):
    with pytest.raises(SeedInputError):
        parse_seed_input(_output_with(path, value))


def test_input_that_is_not_an_object_is_refused():
    with pytest.raises(SeedInputError):
        parse_seed_input(["not", "an", "object"])


def test_the_provider_row_is_an_oidc_provider_named_zitadel_on_the_issuer():
    row = zitadel_provider(issuer_url=f"{ISSUER}/")

    assert (row.name, row.type, row.issuer_url, row.base_url, row.capabilities) == (
        PROVIDER_NAME,
        ProviderType.oidc,
        ISSUER,
        ISSUER,
        ["sso"],
    )
    assert PROVIDER_NAME == "zitadel"
