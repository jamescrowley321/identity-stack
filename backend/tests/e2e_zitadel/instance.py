"""What the Zitadel end-to-end runs read from the applied ``infra/zitadel`` root.

``tests/e2e_zitadel`` and the shared suite's Zitadel run (``tests/e2e`` with
``E2E_PROVIDER=zitadel``) both read the root's outputs from
``ZITADEL_TERRAFORM_OUTPUTS``, which ``make test-e2e-zitadel`` sets.
"""

import json
import os
from dataclasses import dataclass

import pytest

from tests.e2e_zitadel.browser_login import ZitadelUser

OUTPUTS_VARIABLE = "ZITADEL_TERRAFORM_OUTPUTS"
# The outputs the suite reads as objects keyed by name.
_MAP_OUTPUTS = (
    "backend_environment",
    "frontend_environment",
    "test_credentials",
    "organization_ids",
    "test_user_ids",
    "test_user_login_names",
)


@dataclass(frozen=True)
class Instance:
    """What the suite reads from the applied ``infra/zitadel`` root."""

    issuer: str
    spa_client_id: str
    # The scopes the SPA asks for: its application's tokens carry these.
    spa_scope: str
    other_client_id: str
    organization_ids: dict[str, str]
    user_ids: dict[str, str]
    users: dict[str, ZitadelUser]


def read_outputs() -> Instance:
    raw = os.environ.get(OUTPUTS_VARIABLE, "")
    if not raw:
        pytest.fail(f"{OUTPUTS_VARIABLE} is not set: run this suite with `make test-e2e-zitadel` (docs/zitadel.md)")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError(f"expected a JSON object, got {type(parsed).__name__}")
        outputs = {name: output["value"] for name, output in parsed.items()}
        if not outputs:
            pytest.fail(f"{OUTPUTS_VARIABLE} holds no outputs: apply infra/zitadel first (docs/zitadel.md)")
        for name in _MAP_OUTPUTS:
            if not isinstance(outputs[name], dict):
                raise TypeError(f"output {name} is a {type(outputs[name]).__name__}, not an object")
        passwords = outputs["test_credentials"]["users"]
        return Instance(
            issuer=outputs["backend_environment"]["ZITADEL_ISSUER_URL"],
            spa_client_id=outputs["frontend_environment"]["VITE_OIDC_CLIENT_ID"],
            spa_scope=outputs["frontend_environment"]["VITE_OIDC_SCOPE"],
            other_client_id=outputs["other_client_id"],
            organization_ids=outputs["organization_ids"],
            user_ids=outputs["test_user_ids"],
            users={
                key: ZitadelUser(login_name=login_name, password=passwords[key])
                for key, login_name in outputs["test_user_login_names"].items()
            },
        )
    except (ValueError, KeyError, TypeError) as exc:
        pytest.fail(f"{OUTPUTS_VARIABLE} is not the applied infra/zitadel root's outputs ({exc!r})")
