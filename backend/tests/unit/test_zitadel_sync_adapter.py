"""ZitadelSyncAdapter against a fake Zitadel Admin API and fake canonical links.

What reaches Zitadel, in what order, and what is written back to the canonical
model. The live suite (``tests/integration_zitadel/``) runs the adapter against the
compose instance.
"""

import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from app.outbound import UpstreamStatusError
from app.services.adapters.base import CREATED_IN_TENANT, IdentityProviderAdapter, SyncError
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.adapters.zitadel_api import ZitadelAdminApi

PROJECT_ID = "399979368434118659"
ORG_ID = "399979343167631363"
PROVIDER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
TENANT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
SUB = "399979638798954499"
DEFAULT_POLICY = {"allowUsernamePassword": True, "allowRegister": True, "passwordlessType": "PASSWORDLESS_TYPE_ALLOWED"}
ACTIVE = "USER_STATE_ACTIVE"
INACTIVE = "USER_STATE_INACTIVE"


def _refusal(status: int) -> UpstreamStatusError:
    request = httpx.Request("POST", "http://zitadel.localhost:8480/v2/organizations")
    response = httpx.Response(status, request=request, text='{"code":6,"message":"already taken"}')
    return UpstreamStatusError("refused", request=request, response=response)


class FakeApi:
    """Records each call as (method, arguments); ``failing`` names a method that raises."""

    def __init__(self, *, states: dict[str, str] | None = None, failing: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.states = states or {}
        self.failing = failing or {}

    def _record(self, method: str, **arguments) -> None:
        self.calls.append((method, arguments))
        if method in self.failing:
            raise self.failing[method]

    async def add_organization(self, name: str) -> str:
        self._record("add_organization", name=name)
        return ORG_ID

    async def effective_login_policy(self, org_id: str) -> dict:
        self._record("effective_login_policy", org_id=org_id)
        return dict(DEFAULT_POLICY)

    async def add_login_policy(self, org_id: str, policy: dict) -> None:
        self._record("add_login_policy", org_id=org_id, policy=policy)

    async def project_role_keys(self, project_id: str) -> list[str]:
        self._record("project_role_keys", project_id=project_id)
        return ["admin", "member"]

    async def grant_project(self, project_id: str, org_id: str, role_keys: list[str]) -> None:
        self._record("grant_project", project_id=project_id, org_id=org_id, role_keys=role_keys)

    async def add_human_user(self, **arguments) -> None:
        self._record("add_human_user", **arguments)
        self.states[arguments["user_id"]] = ACTIVE

    async def user_state(self, user_id: str) -> str | None:
        self._record("user_state", user_id=user_id)
        return self.states.get(user_id)

    async def deactivate_user(self, user_id: str) -> None:
        self._record("deactivate_user", user_id=user_id)

    async def reactivate_user(self, user_id: str) -> None:
        self._record("reactivate_user", user_id=user_id)

    async def delete_user(self, user_id: str) -> bool:
        self._record("delete_user", user_id=user_id)
        return self.states.pop(user_id, None) is not None


class FakeLinks:
    """The canonical side: the ``zitadel`` provider row, tenant organizations and user subjects."""

    def __init__(
        self,
        *,
        provider_id: uuid.UUID | None = PROVIDER_ID,
        organizations: dict[uuid.UUID, str] | None = None,
        subjects: dict[uuid.UUID, str] | None = None,
    ) -> None:
        self._provider_id = provider_id
        self.organizations = organizations or {}
        self.subjects = subjects or {}
        self.writes: list[tuple] = []

    async def provider_id(self) -> uuid.UUID | None:
        return self._provider_id

    async def organization_of(self, tenant_id: uuid.UUID, provider_id: uuid.UUID) -> str | None:
        assert provider_id == self._provider_id
        return self.organizations.get(tenant_id)

    async def link_organization(self, tenant_id: uuid.UUID, provider_id: uuid.UUID, org_id: str) -> None:
        self.writes.append(("organization", tenant_id, provider_id, org_id))
        self.organizations[tenant_id] = org_id

    async def subject_of(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> str | None:
        assert provider_id == self._provider_id
        return self.subjects.get(user_id)

    async def link_subject(self, user_id: uuid.UUID, provider_id: uuid.UUID, sub: str, email: str) -> None:
        self.writes.append(("subject", user_id, provider_id, sub, email))
        self.subjects[user_id] = sub


class Opener:
    """The adapter's ``open_api``: counts how often a client was opened."""

    def __init__(self, api) -> None:
        self.api = api
        self.opened = 0

    @asynccontextmanager
    async def __call__(self):
        self.opened += 1
        yield self.api


def _adapter(api=None, links=None) -> tuple[ZitadelSyncAdapter, Opener, FakeLinks]:
    opener = Opener(api or FakeApi())
    links = links or FakeLinks()
    return ZitadelSyncAdapter(opener, links, project_id=PROJECT_ID), opener, links


def _created(**overrides) -> dict:
    data = {
        "email": "ada@initech.test",
        "status": "active",
        "user_name": "ada",
        "given_name": "Ada",
        "family_name": "Lovelace",
        CREATED_IN_TENANT: str(TENANT_ID),
    }
    return data | overrides


def test_it_is_an_identity_provider_adapter():
    adapter, _, _ = _adapter()
    assert isinstance(adapter, IdentityProviderAdapter)


class TestSyncTenant:
    async def test_creates_the_organization_closed_to_registration_grants_the_project_then_links_the_tenant(self):
        api = FakeApi()
        adapter, _, links = _adapter(api)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_ok()
        assert api.calls == [
            ("add_organization", {"name": "Initech"}),
            ("effective_login_policy", {"org_id": ORG_ID}),
            ("add_login_policy", {"org_id": ORG_ID, "policy": DEFAULT_POLICY | {"allowRegister": False}}),
            ("project_role_keys", {"project_id": PROJECT_ID}),
            ("grant_project", {"project_id": PROJECT_ID, "org_id": ORG_ID, "role_keys": ["admin", "member"]}),
        ]
        assert links.writes == [("organization", TENANT_ID, PROVIDER_ID, ORG_ID)]

    async def test_without_the_zitadel_provider_row_nothing_reaches_zitadel(self):
        adapter, opener, links = _adapter(links=FakeLinks(provider_id=None))

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_error()
        assert result.error.operation == "sync_tenant"
        assert opener.opened == 0
        assert links.writes == []

    async def test_a_tenant_already_linked_is_left_alone(self):
        adapter, opener, links = _adapter(links=FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_ok()
        assert opener.opened == 0
        assert links.writes == []

    async def test_a_tenant_with_no_name_is_refused_before_any_call(self):
        adapter, opener, _ = _adapter()

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": ""})

        assert result.is_error()
        assert opener.opened == 0

    async def test_an_organization_zitadel_refuses_links_nothing(self):
        api = FakeApi(failing={"add_organization": _refusal(409)})
        adapter, _, links = _adapter(api)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_error()
        assert result.error.context == {"tenant_id": str(TENANT_ID), "status": 409}
        assert links.writes == []

    @pytest.mark.parametrize(
        "failing", ["effective_login_policy", "add_login_policy", "project_role_keys", "grant_project"]
    )
    async def test_a_failure_after_the_organization_exists_links_nothing_and_names_it(self, failing):
        """The tenant is linked last: an organization no tenant names provisions nobody."""
        api = FakeApi(failing={failing: _refusal(403)})
        adapter, _, links = _adapter(api)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_error()
        assert result.error.context == {"tenant_id": str(TENANT_ID), "status": 403, "organization_id": ORG_ID}
        assert links.writes == []


class TestSyncUserCreate:
    async def test_creates_a_human_user_in_the_tenant_organization_and_links_it(self):
        api = FakeApi()
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_ok()
        assert api.calls == [
            (
                "add_human_user",
                {
                    "user_id": str(USER_ID),
                    "org_id": ORG_ID,
                    "username": "ada@initech.test",
                    "given_name": "Ada",
                    "family_name": "Lovelace",
                    "email": "ada@initech.test",
                },
            ),
            ("user_state", {"user_id": str(USER_ID)}),
        ]
        assert links.writes == [("subject", USER_ID, PROVIDER_ID, str(USER_ID), "ada@initech.test")]

    async def test_empty_names_fall_back_to_the_user_name(self):
        """Zitadel refuses an empty given or family name."""
        api = FakeApi()
        adapter, _, _ = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        await adapter.sync_user(user_id=USER_ID, data=_created(given_name="", family_name=""))

        added = dict(api.calls)["add_human_user"]
        assert (added["given_name"], added["family_name"]) == ("ada", "ada")

    async def test_a_user_created_inactive_is_deactivated_after_creation(self):
        api = FakeApi()
        adapter, _, _ = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created(status="inactive"))

        assert result.is_ok()
        assert [method for method, _ in api.calls] == ["add_human_user", "user_state", "deactivate_user"]

    async def test_a_tenant_with_no_zitadel_organization_is_refused_before_any_call(self):
        adapter, opener, links = _adapter()

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_error()
        assert result.error.context == {"user_id": str(USER_ID), "tenant_id": str(TENANT_ID)}
        assert opener.opened == 0
        assert links.writes == []

    async def test_without_the_zitadel_provider_row_nothing_reaches_zitadel(self):
        adapter, opener, _ = _adapter(links=FakeLinks(provider_id=None))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_error()
        assert opener.opened == 0

    async def test_a_user_zitadel_refuses_is_not_linked(self):
        api = FakeApi(failing={"add_human_user": _refusal(400)})
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_error()
        assert result.error.context == {"user_id": str(USER_ID), "status": 400}
        assert links.writes == []

    @pytest.mark.parametrize("missing", ["email", "user_name"])
    async def test_a_user_without_email_or_user_name_is_refused_before_any_call(self, missing):
        adapter, opener, _ = _adapter(links=FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created(**{missing: ""}))

        assert result.is_error()
        assert opener.opened == 0

    async def test_a_linked_user_is_not_created_again(self):
        api = FakeApi(states={SUB: ACTIVE})
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}, subjects={USER_ID: SUB}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_ok()
        assert api.calls == [("user_state", {"user_id": SUB})]
        assert links.writes == []


class TestSyncUserStatus:
    @pytest.mark.parametrize(
        ("status", "state", "expected"),
        [
            ("inactive", ACTIVE, [("user_state", {"user_id": SUB}), ("deactivate_user", {"user_id": SUB})]),
            ("inactive", INACTIVE, [("user_state", {"user_id": SUB})]),
            ("active", INACTIVE, [("user_state", {"user_id": SUB}), ("reactivate_user", {"user_id": SUB})]),
            ("provisioned", INACTIVE, [("user_state", {"user_id": SUB}), ("reactivate_user", {"user_id": SUB})]),
            ("active", ACTIVE, [("user_state", {"user_id": SUB})]),
        ],
    )
    async def test_the_linked_zitadel_user_follows_the_canonical_status(self, status, state, expected):
        api = FakeApi(states={SUB: state})
        adapter, _, links = _adapter(api, FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.sync_user(user_id=USER_ID, data={"email": "ada@initech.test", "status": status})

        assert result.is_ok()
        assert api.calls == expected
        assert links.writes == []

    async def test_a_linked_user_zitadel_no_longer_has_is_an_error(self):
        adapter, _, _ = _adapter(FakeApi(), FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.sync_user(user_id=USER_ID, data={"email": "ada@initech.test", "status": "inactive"})

        assert result.is_error()
        assert result.error.context == {"user_id": str(USER_ID)}

    async def test_an_unlinked_user_that_was_not_just_created_is_an_error_without_a_call(self):
        adapter, opener, _ = _adapter()

        result = await adapter.sync_user(user_id=USER_ID, data={"email": "ada@initech.test", "status": "inactive"})

        assert result.is_error()
        assert result.error.operation == "sync_user"
        assert opener.opened == 0

    async def test_an_unknown_status_is_refused_before_any_call(self):
        adapter, opener, _ = _adapter(links=FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.sync_user(user_id=USER_ID, data={"email": "ada@initech.test", "status": "removed"})

        assert result.is_error()
        assert opener.opened == 0


class TestDeleteUser:
    async def test_removes_the_linked_zitadel_user(self):
        api = FakeApi(states={SUB: ACTIVE})
        adapter, _, _ = _adapter(api, FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.delete_user(user_id=USER_ID)

        assert result.is_ok()
        assert api.calls == [("delete_user", {"user_id": SUB})]

    async def test_a_user_zitadel_no_longer_has_counts_as_removed(self):
        adapter, _, _ = _adapter(FakeApi(), FakeLinks(subjects={USER_ID: SUB}))

        assert (await adapter.delete_user(user_id=USER_ID)).is_ok()

    async def test_an_unlinked_user_is_an_error_without_a_call(self):
        adapter, opener, _ = _adapter()

        result = await adapter.delete_user(user_id=USER_ID)

        assert result.is_error()
        assert opener.opened == 0


class TestCanonicalOnly:
    """Roles, permissions and assignments stay canonical: Ok, and nothing reaches Zitadel."""

    @pytest.mark.parametrize(
        ("method", "arguments"),
        [
            ("sync_role", {"role_id": uuid.uuid4(), "data": {"name": "admin"}}),
            ("sync_permission", {"permission_id": uuid.uuid4(), "data": {"name": "documents:read"}}),
            ("sync_role_assignment", {"user_id": USER_ID, "tenant_id": TENANT_ID, "role_id": uuid.uuid4()}),
            ("delete_role_assignment", {"user_id": USER_ID, "tenant_id": TENANT_ID, "role_id": uuid.uuid4()}),
            ("delete_role", {"role_id": uuid.uuid4()}),
            ("delete_permission", {"permission_id": uuid.uuid4()}),
            ("delete_tenant", {"tenant_id": TENANT_ID}),
        ],
    )
    async def test_returns_ok_and_calls_nothing(self, method, arguments):
        api = FakeApi()
        adapter, opener, links = _adapter(api, FakeLinks(subjects={USER_ID: SUB}))

        result = await getattr(adapter, method)(**arguments)

        assert result.is_ok()
        assert opener.opened == 0
        assert api.calls == []
        assert links.writes == []


class TestZitadelUnreachable:
    """Every method that reaches Zitadel answers Error(SyncError) and raises nothing."""

    @staticmethod
    def _unreachable_adapter(links: FakeLinks) -> ZitadelSyncAdapter:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        @asynccontextmanager
        async def open_api():
            async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
                yield ZitadelAdminApi(http, base_url="http://zitadel.localhost:8480", token="local-test-token")

        return ZitadelSyncAdapter(open_api, links, project_id=PROJECT_ID)

    @pytest.mark.parametrize(
        ("method", "arguments", "links"),
        [
            ("sync_tenant", {"tenant_id": TENANT_ID, "data": {"name": "Initech"}}, {}),
            ("sync_user", {"user_id": USER_ID, "data": _created()}, {"organizations": {TENANT_ID: ORG_ID}}),
            (
                "sync_user",
                {"user_id": USER_ID, "data": {"email": "a@initech.test", "status": "inactive"}},
                {"subjects": {USER_ID: SUB}},
            ),
            ("delete_user", {"user_id": USER_ID}, {"subjects": {USER_ID: SUB}}),
        ],
    )
    async def test_returns_a_sync_error(self, method, arguments, links):
        fake_links = FakeLinks(**links)
        adapter = self._unreachable_adapter(fake_links)

        result = await getattr(adapter, method)(**arguments)

        assert result.is_error()
        assert isinstance(result.error, SyncError)
        assert result.error.operation == method
        assert "local-test-token" not in result.error.message
        assert fake_links.writes == []

    async def test_a_client_that_cannot_be_opened_is_a_sync_error(self):
        @asynccontextmanager
        async def open_api():
            raise RuntimeError("no client")
            yield

        adapter = ZitadelSyncAdapter(open_api, FakeLinks(), project_id=PROJECT_ID)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_error()

    async def test_a_canonical_read_that_fails_is_a_sync_error(self):
        class BrokenLinks(FakeLinks):
            async def provider_id(self):
                raise RuntimeError("database gone")

        adapter, opener, _ = _adapter(links=BrokenLinks())

        for result in [
            await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"}),
            await adapter.sync_user(user_id=USER_ID, data=_created()),
            await adapter.delete_user(user_id=USER_ID),
        ]:
            assert result.is_error()
        assert opener.opened == 0
