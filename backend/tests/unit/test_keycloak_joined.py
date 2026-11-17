"""``KeycloakTokenSource.joined``: membership set up and taken down around a test.

The admin API is an ``httpx.MockTransport``; the real calls are exercised by
the Keycloak e2e suite.
"""

import httpx
import pytest

from tests.helpers.keycloak import KeycloakFlowError, KeycloakTokenSource
from tests.helpers.keycloak_target import KeycloakUser

USER = KeycloakUser("alice", "local-only-alice", "a11ce000-0000-4000-8000-000000000001")
ORGANIZATION = {"id": "0rg00000-0000-4000-8000-000000000002", "alias": "globex"}
MEMBERS = f"/admin/realms/identity-stack/organizations/{ORGANIZATION['id']}/members"


class _AdminApi:
    def __init__(self, *, add: int = 204, remove: int = 204) -> None:
        self.answers = {"POST": add, "DELETE": remove}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/admin/realms/identity-stack/organizations":
            return httpx.Response(200, json=[ORGANIZATION])
        self.calls.append((request.method, request.url.path))
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
