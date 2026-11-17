"""The live Zitadel suite: ZitadelSyncAdapter against the compose instance, with Terraform's outputs.

It needs, all running and none of them optional:

- the compose Zitadel (``make zitadel-up``) with ``infra/zitadel`` applied to it;
- the test Postgres (``make test-up``, ``TEST_DATABASE_URL``);
- the applied root's values in ``ZITADEL_TEST_ISSUER``, ``ZITADEL_TEST_PROJECT_ID`` and
  ``ZITADEL_TEST_SYNC_TOKEN`` (the ``sync_admin_token`` output), and for the machine
  token test ``ZITADEL_TEST_SPA_CLIENT_ID``, ``ZITADEL_TEST_MACHINE_CLIENT_ID`` and
  ``ZITADEL_TEST_MACHINE_CLIENT_SECRET``.

``infra/zitadel/scripts/check-against-compose.sh --integration-tests``
(``make zitadel-terraform-integration``) sets them and runs this suite. A missing value
or an instance that does not answer fails every test: the suite never skips.

It lives apart from ``tests/integration/`` because ``make test-integration`` does not
start Zitadel or apply its Terraform root.
"""

import os

import httpx
import pytest

from app.dependencies.zitadel_sync import ZitadelAdminSettings
from tests.integration.conftest import _run_migrations, async_engine, db_session, postgres_url

# The Postgres fixtures of tests/integration/, shared rather than copied.
__all__ = ["_run_migrations", "async_engine", "db_session", "postgres_url"]

TARGET_VARIABLES = ("ZITADEL_TEST_ISSUER", "ZITADEL_TEST_PROJECT_ID", "ZITADEL_TEST_SYNC_TOKEN")
TIMEOUT_SECONDS = 15.0


@pytest.fixture(scope="session")
def zitadel_settings() -> ZitadelAdminSettings:
    """The adapter's settings for the applied root; fails when a value is missing or Zitadel does not answer."""
    values = {name: os.environ.get(name, "") for name in TARGET_VARIABLES}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.fail(
            f"{', '.join(missing)} not set: run this suite with `make zitadel-terraform-integration` "
            "after `make zitadel-reset` and `make test-up`"
        )
    issuer = values["ZITADEL_TEST_ISSUER"].rstrip("/")
    try:
        httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=TIMEOUT_SECONDS).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"the Zitadel at {issuer} does not answer ({exc!r}): run `make zitadel-reset` first")
    return ZitadelAdminSettings(
        base_url=issuer,
        project_id=values["ZITADEL_TEST_PROJECT_ID"],
        token=values["ZITADEL_TEST_SYNC_TOKEN"],
        timeout_seconds=TIMEOUT_SECONDS,
    )


@pytest.fixture(scope="session")
def zitadel(zitadel_settings) -> httpx.Client:
    """A client that reads the instance back, as the sync adapter's machine user."""
    with httpx.Client(
        base_url=zitadel_settings.base_url,
        headers={"Authorization": f"Bearer {zitadel_settings.token}"},
        timeout=TIMEOUT_SECONDS,
    ) as client:
        yield client
