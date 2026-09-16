"""Unit tests for the E2E leak sweeps (`tests.e2e.helpers.descope_mgmt`).

These run in the unit job, which does not install playwright — hence the sweeps
living in `descope_mgmt` rather than in `auth`, which imports it.

The bug these exist for (#460): the sweeps deleted one object per call and never
looked at the response. Descope rate-limits single deletes per IP, so after a few
dozen calls every further one returned `429 rate_limited_use_batch_delete` — and
those failures were counted as removals. The sweep reported "removed 788" while
removing almost nothing, and the leaked-object backlog grew without bound.
"""

import json

import httpx
import pytest

from tests.e2e.helpers import descope_mgmt as mgmt


class _Recorder:
    """Mock transport handler that keeps every request for later assertions."""

    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def bodies_for(self, path_suffix: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(path_suffix)]

    def methods_for(self, path_suffix: str) -> list[str]:
        return [r.method for r in self.requests if r.url.path.endswith(path_suffix)]


@pytest.fixture
def descope(monkeypatch):
    """Give the sweeps credentials and an in-process Descope to talk to."""
    monkeypatch.setattr(mgmt, "DESCOPE_PROJECT_ID", "P_test")
    monkeypatch.setattr(mgmt, "DESCOPE_MANAGEMENT_KEY", "K_test")
    monkeypatch.setattr(mgmt, "E2E_TEST_TENANT_ID", "T_test")
    monkeypatch.setattr(mgmt, "E2E_TEST_EMAIL", "keep-me@example.com")

    real_client = httpx.Client

    def install(handler) -> _Recorder:
        recorder = _Recorder(handler)

        def factory(*_args, **kwargs):
            kwargs.pop("timeout", None)
            return real_client(transport=httpx.MockTransport(recorder), **kwargs)

        monkeypatch.setattr(mgmt.httpx, "Client", factory)
        return recorder

    return install


def _rbac_handler(*, delete_response):
    """Two leaked objects and two real-model objects, per kind."""
    listings = {
        "/v1/mgmt/role/all": {
            "roles": [
                {"name": "list-role-e2e-abc123"},
                {"name": "admin"},
                {"name": "Tenant Admin"},
            ]
        },
        "/v1/mgmt/permission/all": {
            "permissions": [
                {"name": "docs.read-e2e-def456"},
                {"name": "projects.read"},
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in listings:
            assert request.method == "GET", f"{path} must be listed with GET, got {request.method}"
            return httpx.Response(200, json=listings[path])
        if path.endswith("/delete/batch"):
            return delete_response()
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return handler


def test_rbac_sweep_deletes_through_the_batch_endpoints(descope):
    """Marked names go out in one batch call per kind, with the go-sdk body shape."""
    recorder = descope(_rbac_handler(delete_response=lambda: httpx.Response(200, json={})))

    assert mgmt.sweep_leaked_e2e_rbac() == 2

    assert recorder.bodies_for("/role/delete/batch") == [
        {"roleNames": ["list-role-e2e-abc123"], "tenantId": "", "roleIds": []}
    ]
    assert recorder.bodies_for("/permission/delete/batch") == [{"names": ["docs.read-e2e-def456"], "ids": []}]
    # The single-object endpoints are what Descope rate-limits; nothing may use them.
    assert recorder.methods_for("/v1/mgmt/role/delete") == []
    assert recorder.methods_for("/v1/mgmt/permission/delete") == []


def test_rate_limited_batch_is_not_counted_as_removed(descope):
    """The #460 regression: a 429 batch removed nothing and must report nothing."""
    rate_limited = lambda: httpx.Response(  # noqa: E731 - one-liner stub
        429,
        json={"error": {"code": "rate_limited_use_batch_delete", "message": "Use the batch endpoint"}},
    )
    descope(_rbac_handler(delete_response=rate_limited))

    assert mgmt.sweep_leaked_e2e_rbac() == 0


def test_rbac_sweep_never_touches_the_real_model(descope):
    """Only `-e2e-<hex>` names are eligible; a project with no debris is left alone."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mgmt/role/all":
            return httpx.Response(200, json={"roles": [{"name": n} for n in ("admin", "owner", "viewer")]})
        if request.url.path == "/v1/mgmt/permission/all":
            return httpx.Response(200, json={"permissions": [{"name": "projects.read"}]})
        raise AssertionError(f"nothing should be deleted, got {request.method} {request.url.path}")

    recorder = descope(handler)

    assert mgmt.sweep_leaked_e2e_rbac() == 0
    assert recorder.bodies_for("/delete/batch") == []


def test_rbac_sweep_skips_a_kind_whose_listing_fails(descope):
    """A failed listing must not be read as 'no debris' for the other kind."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mgmt/role/all":
            return httpx.Response(500, json={})
        if request.url.path == "/v1/mgmt/permission/all":
            return httpx.Response(200, json={"permissions": [{"name": "docs.read-e2e-def456"}]})
        if request.url.path.endswith("/delete/batch"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    recorder = descope(handler)

    assert mgmt.sweep_leaked_e2e_rbac() == 1
    assert recorder.bodies_for("/role/delete/batch") == []
    assert recorder.bodies_for("/permission/delete/batch") == [{"names": ["docs.read-e2e-def456"], "ids": []}]


def test_access_key_sweep_batches_ids(descope):
    """Only `e2e-admin-` keys are swept, and they go out as one batch of ids."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mgmt/accesskey/search":
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {"id": "K_leak", "name": "e2e-admin-deadbeef"},
                        {"id": "K_real", "name": "production-integration"},
                        {"id": "", "name": "e2e-admin-noid"},
                    ]
                },
            )
        if request.url.path == "/v1/mgmt/accesskey/delete/batch":
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    recorder = descope(handler)

    assert mgmt.sweep_leaked_e2e_access_keys() == 1
    assert recorder.bodies_for("/accesskey/delete/batch") == [{"ids": ["K_leak"]}]


def test_access_key_sweep_reports_zero_when_the_batch_fails(descope):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mgmt/accesskey/search":
            return httpx.Response(200, json={"keys": [{"id": "K_leak", "name": "e2e-admin-deadbeef"}]})
        return httpx.Response(429, json={"error": {"code": "rate_limited_use_batch_delete"}})

    descope(handler)

    assert mgmt.sweep_leaked_e2e_access_keys() == 0


def test_user_sweep_batches_user_ids_and_spares_the_fixture_user(descope):
    """Keyed on userId (what the batch endpoint takes), never on the reused test user."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/mgmt/user/search":
            return httpx.Response(
                200,
                json={
                    "users": [
                        {"userId": "U_leak", "loginIds": ["e2e-invite-abc@example.com"]},
                        {"userId": "U_leak2", "loginIds": ["e2e-lifecycle-def@example.com"]},
                        {"userId": "U_keep", "loginIds": ["keep-me@example.com"]},
                        {"userId": "U_real", "loginIds": ["someone@example.com"]},
                        {"userId": "", "loginIds": ["e2e-invite-noid@example.com"]},
                    ]
                },
            )
        if request.url.path == "/v1/mgmt/user/delete/batch":
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    recorder = descope(handler)

    assert mgmt.sweep_leaked_e2e_users() == 2
    assert recorder.bodies_for("/user/delete/batch") == [{"userIds": ["U_leak", "U_leak2"]}]


def test_large_backlog_is_chunked_and_fully_counted(descope):
    """A backlog past one batch is split, and every successful chunk counts once."""
    leaked = [{"name": f"role-{i}-e2e-{i:06x}"} for i in range(250)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/mgmt/role/all":
            return httpx.Response(200, json={"roles": leaked})
        if request.url.path == "/v1/mgmt/permission/all":
            return httpx.Response(200, json={"permissions": []})
        if request.url.path.endswith("/delete/batch"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    recorder = descope(handler)

    assert mgmt.sweep_leaked_e2e_rbac() == 250
    sent = recorder.bodies_for("/role/delete/batch")
    assert [len(b["roleNames"]) for b in sent] == [100, 100, 50]
    assert [n for b in sent for n in b["roleNames"]] == [r["name"] for r in leaked]
