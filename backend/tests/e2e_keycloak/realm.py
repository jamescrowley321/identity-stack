"""The canonical model brought to the realm's state before a Keycloak end-to-end run.

``make keycloak-seed`` links the tenants and the fixture users; their role
assignments and the realm's permissions and roles come in through reconciliation
(``POST /api/internal/reconciliation/run``), as for any deployment that syncs
with Keycloak. Both suites run it before their first test: it also repairs what
an interrupted earlier run left behind.
"""

import os

import httpx
import pytest

KEYCLOAK = "keycloak"
RECONCILIATION = "/api/internal/reconciliation/run"
# A fresh database needs two applying passes (roles, then the role groups of the
# roles the first created) and a third that applies nothing; one more is slack.
MAX_RECONCILIATION_PASSES = 4
_CONFIGURATION = (
    "The backend must run with SYNC_PROVIDER=keycloak and the suite with its DESCOPE_FLOW_SYNC_SECRET "
    "(docs/keycloak.md#running-the-end-to-end-suite-locally)"
)
_TIMEOUT_SECONDS = 60


def take_in_the_realm(backend_url: str) -> None:
    """Run reconciliation passes until one applies nothing: Postgres then holds what the realm holds.

    A refused or failed pass, or passes that keep applying changes, fail the run.
    """
    passes = []
    for _ in range(MAX_RECONCILIATION_PASSES):
        try:
            response = httpx.post(
                f"{backend_url}{RECONCILIATION}",
                headers={"X-Flow-Secret": os.environ.get("DESCOPE_FLOW_SYNC_SECRET", "")},
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            pytest.fail(f"POST {RECONCILIATION} got no answer ({exc!r}). {_CONFIGURATION}")
        if response.status_code != httpx.codes.OK:
            pytest.fail(f"POST {RECONCILIATION} answered {response.status_code}: {response.text}. {_CONFIGURATION}")
        report = response.json().get("providers", {}).get(KEYCLOAK)
        if not isinstance(report, dict) or "stats" not in report:
            pytest.fail(f"POST {RECONCILIATION} ran no completed Keycloak pass: {response.text}. {_CONFIGURATION}")
        stats = report["stats"]
        passes.append(stats)
        if stats["failed"] or stats["push_failed"]:
            pytest.fail(f"a reconciliation pass with Keycloak failed: {stats}")
        if not stats["applied"]:
            return
    pytest.fail(f"Keycloak reconciliation did not converge in {len(passes)} passes: {passes}")
