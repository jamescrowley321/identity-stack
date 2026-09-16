"""Descope Management API access shared by the E2E helpers, plus the leak sweeps.

Split out of ``auth.py`` so the sweeps can be unit-tested: ``auth.py`` imports
``playwright.sync_api``, which the unit job does not install (it installs the
``[dev]`` extra; playwright lives in the ``dev`` dependency-group used by the
E2E job). Nothing here imports playwright.
"""

import os
import re
from collections.abc import Callable, Iterable, Sequence
from http import HTTPStatus

import httpx

DESCOPE_BASE_URL = os.environ.get("DESCOPE_BASE_URL", "https://api.descope.com")
DESCOPE_PROJECT_ID = os.environ.get("DESCOPE_PROJECT_ID", "")
DESCOPE_MANAGEMENT_KEY = os.environ.get("DESCOPE_MANAGEMENT_KEY", "")

E2E_TEST_EMAIL = os.environ.get("E2E_TEST_EMAIL", "")
E2E_TEST_TENANT_ID = os.environ.get("E2E_TEST_TENANT_ID", "")


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {DESCOPE_PROJECT_ID}:{DESCOPE_MANAGEMENT_KEY}"}


def _mgmt_url(path: str) -> str:
    return f"{DESCOPE_BASE_URL}{path}"


# Login-id prefixes for users the E2E suite creates as fixtures. Anything
# matching these is disposable by construction — the suffix is a random uuid4
# fragment, so a surviving one can only be litter from an earlier run.
LEAKED_USER_PREFIXES = ("e2e-invite-", "e2e-lifecycle-")

# Name prefix for the throwaway owner+admin key `get_admin_session_token` mints
# once per session. The suffix is a uuid4 fragment, so a survivor can only be
# litter from an earlier run.
LEAKED_ACCESS_KEY_PREFIX = "e2e-admin-"

# E2E-created roles and permissions all carry an `-e2e-<hex>` fragment from
# `unique_name`. Matching on that rather than on a leading prefix, because the
# fixtures use many different prefixes (list-, chain-, batch-, canon-, upd-, …)
# and a prefix list would silently miss whichever one gets added next.
_E2E_RBAC_MARKER = re.compile(r"-e2e-[0-9a-f]{6,}")

# Descope caps how many objects one batch-delete request accepts; 100 keeps the
# request small while collapsing a 700-object backlog into a handful of calls.
_BATCH_SIZE = 100


def _delete_in_batches(
    client: httpx.Client,
    *,
    kind: str,
    path: str,
    body_for: Callable[[list[str]], dict],
    keys: Sequence[str],
) -> int:
    """Delete `keys` through a batch endpoint. Returns how many were ACTUALLY removed.

    Descope rate-limits single-object deletes per source IP: after a few dozen
    calls, every further one answers
    ``429 {"code": "rate_limited_use_batch_delete"}``. The sweeps used to delete
    one per call and never look at the response, so they counted those failures
    as removals — reporting "removed 788" while removing almost nothing, and the
    debris grew without bound (2227 objects at one point, ~790 again eleven days
    later). See #460.

    A non-200 batch therefore removes NOTHING and is counted as nothing, and it
    says so on stdout rather than being absorbed into the total.
    """
    if not keys:
        return 0

    removed = 0
    survived = 0
    for start in range(0, len(keys), _BATCH_SIZE):
        chunk = list(keys[start : start + _BATCH_SIZE])
        response = client.post(_mgmt_url(path), headers=_auth_header(), json=body_for(chunk))
        if response.status_code != HTTPStatus.OK:
            survived += len(chunk)
            print(
                f"[E2E] {kind} sweep: batch delete of {len(chunk)} returned "
                f"HTTP {response.status_code} {response.text[:200]!r} — none of that batch removed"
            )
            continue
        removed += len(chunk)

    if survived:
        print(
            f"[E2E] {kind} sweep: {survived} object(s) survived and will be retried next run. "
            "If this number keeps growing, the sweep is not keeping up with the leak."
        )
    return removed


# (kind, list path, response key, batch-delete path, request body builder).
# Roles first: deleting a permission still referenced by a role can be refused,
# and every leaked role is itself disposable.
_RBAC_SWEEP_TARGETS = (
    (
        "role",
        "/v1/mgmt/role/all",
        "roles",
        "/v1/mgmt/role/delete/batch",
        lambda names: {"roleNames": names, "tenantId": "", "roleIds": []},
    ),
    (
        "permission",
        "/v1/mgmt/permission/all",
        "permissions",
        "/v1/mgmt/permission/delete/batch",
        lambda names: {"names": names, "ids": []},
    ),
)


def _marked_names(items: Iterable[dict]) -> list[str]:
    """Names carrying the E2E marker. The real model never matches."""
    names = []
    for item in items:
        name = item.get("name") or ""
        if _E2E_RBAC_MARKER.search(name):
            names.append(name)
    return names


def sweep_leaked_e2e_rbac() -> int:
    """Delete roles and permissions left behind by earlier runs.

    Per-test cleanup deletes by name inside a `finally`, which never runs for a
    cancelled run and is skipped whenever a test fails before the name is bound.
    The debris is unbounded and it makes the project's real authorization model
    unreadable. Only names carrying the `-e2e-<hex>` marker are touched, so the
    real model (`Tenant Admin`, `viewer`, `projects.*`, …) is never at risk.

    Note the listing endpoints are GET. A POST to `/v1/mgmt/role/all` answers
    200 with an empty `roles` list rather than 405, so a wrong method here reads
    as "the project has no roles" and the sweep silently does nothing.
    """
    if not (DESCOPE_PROJECT_ID and DESCOPE_MANAGEMENT_KEY):
        return 0

    removed = 0
    with httpx.Client(timeout=60) as client:
        for kind, list_path, list_key, delete_path, body_for in _RBAC_SWEEP_TARGETS:
            response = client.get(_mgmt_url(list_path), headers=_auth_header())
            if response.status_code != HTTPStatus.OK:
                print(f"[E2E] rbac sweep: {kind} list returned {response.status_code}, skipping")
                continue
            removed += _delete_in_batches(
                client,
                kind=kind,
                path=delete_path,
                body_for=body_for,
                keys=_marked_names(response.json().get(list_key, [])),
            )

    if removed:
        print(f"[E2E] rbac sweep: removed {removed} leaked role(s)/permission(s)")
    return removed


def sweep_leaked_e2e_access_keys() -> int:
    """Delete owner/admin access keys left behind by earlier runs.

    `get_admin_session_token` mints one per session and deletes it in a
    `finally`. A failed delete used to be swallowed whole, leaving a standing
    owner+admin credential with no expiry on a shared project. This is the
    backstop for that, and for runs killed before teardown.
    """
    if not (DESCOPE_PROJECT_ID and DESCOPE_MANAGEMENT_KEY and E2E_TEST_TENANT_ID):
        return 0

    with httpx.Client(timeout=30) as client:
        response = client.post(
            _mgmt_url("/v1/mgmt/accesskey/search"),
            headers=_auth_header(),
            json={"tenantIds": [E2E_TEST_TENANT_ID]},
        )
        if response.status_code != HTTPStatus.OK:
            print(f"[E2E] access-key sweep: search returned {response.status_code}, skipping")
            return 0

        key_ids = [
            key_id
            for key in response.json().get("keys", [])
            if (key_id := key.get("id") or "") and (key.get("name") or "").startswith(LEAKED_ACCESS_KEY_PREFIX)
        ]
        removed = _delete_in_batches(
            client,
            kind="access-key",
            path="/v1/mgmt/accesskey/delete/batch",
            body_for=lambda ids: {"ids": ids},
            keys=key_ids,
        )

    if removed:
        print(f"[E2E] access-key sweep: removed {removed} leaked admin key(s)")
    return removed


def sweep_leaked_e2e_users() -> int:
    """Delete users left behind by earlier E2E runs. Returns the count removed.

    Per-test cleanup deletes by user id, which means it cannot run when the
    invite response does not carry one — notably the 207 path, where Descope
    has already created the user but the body is an RFC 9457 Problem Detail.
    Those users then survive forever and count against the project's user
    limit. This sweeps by login-id prefix instead, so it also catches users
    stranded by a crashed or cancelled run.

    Never touches E2E_TEST_EMAIL: that user is provisioned deliberately and is
    reused across runs.
    """
    if not (DESCOPE_PROJECT_ID and DESCOPE_MANAGEMENT_KEY):
        return 0

    with httpx.Client(timeout=30) as client:
        response = client.post(
            _mgmt_url("/v2/mgmt/user/search"),
            headers=_auth_header(),
            json={"limit": 500, "page": 0},
        )
        if response.status_code != HTTPStatus.OK:
            print(f"[E2E] user sweep: search returned {response.status_code}, skipping")
            return 0

        # The batch endpoint keys on userIds, not loginIds — a user with no
        # userId in the search payload cannot be batched and is left for the
        # next run rather than being counted as removed.
        user_ids = []
        for user in response.json().get("users", []):
            login_ids = user.get("loginIds") or []
            if not login_ids or login_ids[0] == E2E_TEST_EMAIL:
                continue
            if not login_ids[0].startswith(LEAKED_USER_PREFIXES):
                continue
            user_id = user.get("userId") or ""
            if user_id:
                user_ids.append(user_id)

        removed = _delete_in_batches(
            client,
            kind="user",
            path="/v1/mgmt/user/delete/batch",
            body_for=lambda ids: {"userIds": ids},
            keys=user_ids,
        )

    if removed:
        print(f"[E2E] user sweep: removed {removed} leaked user(s)")
    return removed
