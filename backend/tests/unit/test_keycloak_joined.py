"""``KeycloakTokenSource.joined`` and ``disabled``: realm changes set up and taken down around a test.

The admin API is an ``httpx.MockTransport``; the real calls are exercised by
the Keycloak e2e suite.
"""

import json

import httpx
import pytest

from tests.helpers.keycloak import KeycloakFlowError, KeycloakTokenSource
from tests.helpers.keycloak_target import KeycloakUser

USER = KeycloakUser("alice", "local-only-alice", "a11ce000-0000-4000-8000-000000000001")
ORGANIZATION = {"id": "0rg00000-0000-4000-8000-000000000002", "alias": "globex"}
MEMBERS = f"/admin/realms/identity-stack/organizations/{ORGANIZATION['id']}/members"


GROUPS = f"/admin/realms/identity-stack/organizations/{ORGANIZATION['id']}/groups"
ADMIN_GROUP = {"id": "9a020000-0000-4000-8000-000000000002", "name": "admin"}
ACCOUNT = f"/admin/realms/identity-stack/users/{USER.id}"


class _AdminApi:
    def __init__(self, *, add: int = 204, remove: int = 204, put: int = 204) -> None:
        self.answers = {"POST": add, "DELETE": remove, "PUT": put}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/admin/realms/identity-stack/organizations":
            return httpx.Response(200, json=[ORGANIZATION])
        if request.method == "GET" and request.url.path == GROUPS:
            return httpx.Response(200, json=[{"id": "other", "name": "member"}, ADMIN_GROUP])
        if request.method == "GET" and request.url.path == ACCOUNT:
            return httpx.Response(200, json={"id": USER.id, "username": USER.username, "enabled": True})
        self.calls.append((request.method, request.url.path))
        if request.content:
            self.bodies.append(json.loads(request.content))
        return httpx.Response(self.answers[request.method])


class _Source(KeycloakTokenSource):
    def __init__(self, api: _AdminApi) -> None:
        super().__init__(base_url="http://keycloak.test", realm="identity-stack")
        self._api = api

    def _admin(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"{self.base_url}/admin/realms/{self.realm}", transport=httpx.MockTransport(self._api)
        )


def test_the_user_is_added_then_removed():
    api = _AdminApi()

    with _Source(api).joined(USER, "globex"):
        assert api.calls == [("POST", MEMBERS)]

    assert api.calls == [("POST", MEMBERS), ("DELETE", f"{MEMBERS}/{USER.id}")]


def test_a_user_already_a_member_says_to_recreate_the_container():
    api = _AdminApi(add=409)

    with pytest.raises(KeycloakFlowError, match="recreate keycloak-test") as refused:
        with _Source(api).joined(USER, "globex"):
            pytest.fail("the body must not run")

    assert "make test-down && make test-up-keycloak" in str(refused.value)
    assert api.calls == [("POST", MEMBERS)]


def test_a_failed_removal_does_not_hide_the_failure_of_the_body():
    api = _AdminApi(remove=500)

    with pytest.raises(AssertionError, match="the real failure") as failed:
        with _Source(api).joined(USER, "globex"):
            raise AssertionError("the real failure")

    assert any("500" in note for note in failed.value.__notes__)
    assert api.calls == [("POST", MEMBERS), ("DELETE", f"{MEMBERS}/{USER.id}")]


def test_a_failed_removal_after_a_passing_body_fails():
    api = _AdminApi(remove=500)

    with pytest.raises(httpx.HTTPStatusError):
        with _Source(api).joined(USER, "globex"):
            pass


def test_a_role_puts_the_user_in_the_organizations_group_for_it():
    api = _AdminApi()

    with _Source(api).joined(USER, "globex", role="admin"):
        assert api.calls == [("POST", MEMBERS), ("PUT", f"{GROUPS}/{ADMIN_GROUP['id']}/members/{USER.id}")]

    assert api.calls[-1] == ("DELETE", f"{MEMBERS}/{USER.id}")


def test_a_refused_group_membership_takes_the_user_out_again():
    api = _AdminApi(put=400)

    with pytest.raises(httpx.HTTPStatusError):
        with _Source(api).joined(USER, "globex", role="admin"):
            pytest.fail("the body must not run")

    assert api.calls[-1] == ("DELETE", f"{MEMBERS}/{USER.id}")


def test_disabled_disables_the_account_then_enables_it():
    api = _AdminApi()

    with _Source(api).disabled(USER):
        assert [body["enabled"] for body in api.bodies] == [False]

    assert [body["enabled"] for body in api.bodies] == [False, True]
    assert api.calls == [("PUT", ACCOUNT), ("PUT", ACCOUNT)]


def test_disabled_enables_the_account_after_a_failing_body():
    api = _AdminApi()

    with pytest.raises(AssertionError, match="the real failure"):
        with _Source(api).disabled(USER):
            raise AssertionError("the real failure")

    assert [body["enabled"] for body in api.bodies] == [False, True]
