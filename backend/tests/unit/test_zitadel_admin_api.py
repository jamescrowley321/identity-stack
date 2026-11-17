"""ZitadelAdminApi: the requests it sends and how it reads Zitadel's answers.

The shapes are the ones the compose instance (Zitadel v4.20.3) answers; the live
suite in ``tests/integration_zitadel/`` runs the same calls against it.
"""

import json

import httpx
import pytest

from app.outbound import UpstreamStatusError, UpstreamTransportError
from app.services.adapters.zitadel_api import (
    LOGIN_POLICY_FIELDS,
    ZitadelAdminApi,
    ZitadelUser,
    login_policy_without_registration,
)

BASE_URL = "http://zitadel.localhost:8480"
TOKEN = "local-test-token"
ORG_ID = "399979343167631363"
PROJECT_ID = "399979368434118659"
USER_ID = "6b1f1c1e-0000-4000-8000-000000000001"


class Recorder:
    """A MockTransport handler that answers by (method, path) and records each request."""

    def __init__(self, answers: dict[tuple[str, str], httpx.Response]) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answers[(request.method, request.url.path)]

    def only(self) -> httpx.Request:
        assert len(self.requests) == 1
        return self.requests[0]


def _api(recorder: Recorder) -> ZitadelAdminApi:
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return ZitadelAdminApi(http, base_url=BASE_URL, token=TOKEN)


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


async def test_add_organization_posts_its_id_and_name():
    recorder = Recorder(
        {("POST", "/v2/organizations"): httpx.Response(201, json={"organizationId": ORG_ID, "details": {}})}
    )

    await _api(recorder).add_organization(ORG_ID, "Initech")

    request = recorder.only()
    assert str(request.url) == f"{BASE_URL}/v2/organizations"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert _body(request) == {"organizationId": ORG_ID, "name": "Initech"}


@pytest.mark.parametrize(("result", "expected"), [([{"id": ORG_ID, "name": "Initech"}], True), ([], False)])
async def test_has_organization_searches_by_id(result, expected):
    answer = {"details": {"totalResult": str(len(result))}} | ({"result": result} if result else {})
    recorder = Recorder({("POST", "/v2/organizations/_search"): httpx.Response(200, json=answer)})

    assert await _api(recorder).has_organization(ORG_ID) is expected
    assert _body(recorder.only()) == {"queries": [{"idQuery": {"id": ORG_ID}}]}


async def test_effective_login_policy_reads_it_in_the_organization():
    policy = {"allowUsernamePassword": True, "allowRegister": True, "isDefault": True}
    recorder = Recorder(
        {("GET", "/management/v1/policies/login"): httpx.Response(200, json={"policy": policy, "isDefault": True})}
    )

    answered = await _api(recorder).effective_login_policy(ORG_ID)

    assert answered == policy
    assert recorder.only().headers["x-zitadel-orgid"] == ORG_ID


async def test_add_login_policy_posts_it_in_the_organization():
    recorder = Recorder({("POST", "/management/v1/policies/login"): httpx.Response(200, json={"details": {}})})

    await _api(recorder).add_login_policy(ORG_ID, {"allowRegister": False})

    request = recorder.only()
    assert request.headers["x-zitadel-orgid"] == ORG_ID
    assert _body(request) == {"allowRegister": False}


def test_login_policy_without_registration_copies_every_known_field_and_turns_registration_off():
    effective = {field: f"value-{field}" for field in LOGIN_POLICY_FIELDS}
    effective |= {"details": {"sequence": "19"}, "isDefault": True, "idps": [{"idpId": "1"}], "allowRegister": True}

    policy = login_policy_without_registration(effective)

    assert policy == {field: f"value-{field}" for field in LOGIN_POLICY_FIELDS} | {"allowRegister": False}


def test_login_policy_fields_keep_password_login():
    """A field left out is zeroed by Zitadel: without allowUsernamePassword nobody could log in."""
    assert {"allowUsernamePassword", "allowRegister", "passwordlessType", "secondFactors"} <= LOGIN_POLICY_FIELDS


async def test_project_role_keys_lists_the_project_roles():
    recorder = Recorder(
        {
            ("POST", "/zitadel.project.v2.ProjectService/ListProjectRoles"): httpx.Response(
                200,
                json={
                    "pagination": {"totalResult": "2"},
                    "projectRoles": [{"key": "member", "projectId": PROJECT_ID}, {"key": "admin"}],
                },
            )
        }
    )

    keys = await _api(recorder).project_role_keys(PROJECT_ID)

    assert keys == ["admin", "member"]
    assert _body(recorder.only()) == {"projectId": PROJECT_ID}


async def test_project_role_keys_of_a_project_with_no_roles_is_empty():
    recorder = Recorder(
        {
            ("POST", "/zitadel.project.v2.ProjectService/ListProjectRoles"): httpx.Response(
                200, json={"pagination": {"totalResult": "0"}}
            )
        }
    )

    assert await _api(recorder).project_role_keys(PROJECT_ID) == []


async def test_grant_project_grants_it_to_the_organization_with_the_roles():
    recorder = Recorder(
        {("POST", "/zitadel.project.v2.ProjectService/CreateProjectGrant"): httpx.Response(200, json={})}
    )

    await _api(recorder).grant_project(PROJECT_ID, ORG_ID, ["admin", "member"])

    assert _body(recorder.only()) == {
        "projectId": PROJECT_ID,
        "grantedOrganizationId": ORG_ID,
        "roleKeys": ["admin", "member"],
    }


async def test_grant_project_that_the_organization_already_has_counts_as_granted():
    recorder = Recorder(
        {
            ("POST", "/zitadel.project.v2.ProjectService/CreateProjectGrant"): httpx.Response(
                409, json={"code": "already_exists", "message": "Project Grant already exists (V3-DKcYh)"}
            )
        }
    )

    await _api(recorder).grant_project(PROJECT_ID, ORG_ID, ["admin", "member"])


async def test_grant_project_refused_otherwise_raises():
    recorder = Recorder(
        {("POST", "/zitadel.project.v2.ProjectService/CreateProjectGrant"): httpx.Response(403, json={})}
    )

    with pytest.raises(UpstreamStatusError):
        await _api(recorder).grant_project(PROJECT_ID, ORG_ID, ["admin", "member"])


async def test_add_human_user_creates_it_in_the_organization_with_its_own_id():
    recorder = Recorder({("POST", "/v2/users/human"): httpx.Response(200, json={"userId": USER_ID})})

    await _api(recorder).add_human_user(
        user_id=USER_ID,
        org_id=ORG_ID,
        username="ada@initech.test",
        given_name="Ada",
        family_name="Lovelace",
        email="ada@initech.test",
    )

    assert _body(recorder.only()) == {
        "userId": USER_ID,
        "organization": {"orgId": ORG_ID},
        "username": "ada@initech.test",
        "profile": {"givenName": "Ada", "familyName": "Lovelace"},
        "email": {"email": "ada@initech.test", "isVerified": True},
    }


async def test_find_user_reads_its_state_and_organization():
    answer = {
        "details": {"resourceOwner": ORG_ID},
        "user": {"userId": USER_ID, "details": {"resourceOwner": ORG_ID}, "state": "USER_STATE_INACTIVE"},
    }
    recorder = Recorder({("GET", f"/v2/users/{USER_ID}"): httpx.Response(200, json=answer)})

    assert await _api(recorder).find_user(USER_ID) == ZitadelUser(state="USER_STATE_INACTIVE", organization_id=ORG_ID)


async def test_find_user_of_a_user_zitadel_does_not_have_is_none():
    recorder = Recorder({("GET", f"/v2/users/{USER_ID}"): httpx.Response(404, json={"code": 5})})

    assert await _api(recorder).find_user(USER_ID) is None


@pytest.mark.parametrize(("method", "action"), [("deactivate_user", "deactivate"), ("reactivate_user", "reactivate")])
async def test_state_changes_post_to_the_user(method, action):
    recorder = Recorder({("POST", f"/v2/users/{USER_ID}/{action}"): httpx.Response(200, json={"details": {}})})

    await getattr(_api(recorder), method)(USER_ID)

    assert _body(recorder.only()) == {}


async def test_delete_user_answers_true_when_it_removed_the_user():
    recorder = Recorder({("DELETE", f"/v2/users/{USER_ID}"): httpx.Response(200, json={"details": {}})})

    assert await _api(recorder).delete_user(USER_ID) is True


async def test_delete_user_answers_false_when_the_user_was_already_gone():
    recorder = Recorder({("DELETE", f"/v2/users/{USER_ID}"): httpx.Response(404, json={"code": 5})})

    assert await _api(recorder).delete_user(USER_ID) is False


async def test_a_refusal_raises_the_upstream_status_error():
    recorder = Recorder({("POST", "/v2/organizations"): httpx.Response(409, json={"code": 6})})

    with pytest.raises(UpstreamStatusError) as raised:
        await _api(recorder).add_organization(ORG_ID, "Initech")

    assert raised.value.response.status_code == 409


async def test_a_user_id_is_sent_as_one_path_segment():
    """An id is quoted into the path, so it cannot name another endpoint."""
    # httpx decodes url.path, so the answer is keyed decoded; raw_path is what was sent.
    recorder = Recorder({("GET", "/v2/users/a/deactivate"): httpx.Response(404, json={})})

    assert await _api(recorder).find_user("a/deactivate") is None
    assert recorder.only().url.raw_path == b"/v2/users/a%2Fdeactivate"


async def test_no_answer_raises_the_upstream_transport_error():
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    api = ZitadelAdminApi(httpx.AsyncClient(transport=httpx.MockTransport(unreachable)), base_url=BASE_URL, token=TOKEN)

    with pytest.raises(UpstreamTransportError):
        await api.add_organization(ORG_ID, "Initech")
