"""ZitadelSyncAdapter against a fake Zitadel Admin API and fake canonical links.

What reaches Zitadel, in what order, and what is written back to the canonical
model. The live suite (``tests/integration_zitadel/``) runs the adapter against the
compose instance.
"""

import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from app.outbound import UpstreamStatusError, UpstreamTransportError
from app.services.adapters.base import (
    CREATED_IN_TENANT,
    LAST_ROLE_IN_TENANT,
    STATUS_CHANGED,
    UNANSWERED,
    IdentityProviderAdapter,
    SyncError,
)
from app.services.adapters.zitadel import ZitadelSyncAdapter
from app.services.adapters.zitadel_api import ZitadelAdminApi, ZitadelUser

PROJECT_ID = "399979368434118659"
PROVIDER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
TENANT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
USER_ID = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
# The adapter gives a tenant's organization the tenant's id (a seeded one has Zitadel's own).
ORG_ID = str(TENANT_ID)
OTHER_ORG_ID = "399979343167631999"
SUB = "399979638798954499"
DEFAULT_POLICY = {
    "allowUsernamePassword": True,
    "allowRegister": True,
    "passwordlessType": "PASSWORDLESS_TYPE_ALLOWED",
    "isDefault": True,
}
OWN_POLICY = {"allowUsernamePassword": True, "passwordlessType": "PASSWORDLESS_TYPE_ALLOWED"}
ACTIVE = "USER_STATE_ACTIVE"
INACTIVE = "USER_STATE_INACTIVE"
LOCKED = "USER_STATE_LOCKED"
INITIAL = "USER_STATE_INITIAL"


def _refusal(status: int) -> UpstreamStatusError:
    request = httpx.Request("POST", "http://zitadel.localhost:8480/v2/organizations")
    response = httpx.Response(status, request=request, text='{"code":6,"message":"already taken"}')
    return UpstreamStatusError("refused", request=request, response=response)


def _lost() -> UpstreamTransportError:
    request = httpx.Request("POST", "http://zitadel.localhost:8480/v2/users/human")
    return UpstreamTransportError("read timed out", request=request)


class FakeApi:
    """Records each call as (method, arguments); ``failing`` names a method that raises.

    ``users`` maps a Zitadel user id to its ZitadelUser, ``organizations`` holds the
    organization ids Zitadel has, and ``policy`` is the login policy an organization has.
    A failing create still creates when ``created_anyway`` (its answer was lost).
    """

    def __init__(
        self,
        *,
        users: dict[str, ZitadelUser] | None = None,
        organizations: set[str] | None = None,
        policy: dict | None = None,
        failing: dict[str, Exception] | None = None,
        created_anyway: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.users = users or {}
        self.organizations = organizations or set()
        self.policy = dict(DEFAULT_POLICY) if policy is None else policy
        self.failing = failing or {}
        self.created_anyway = created_anyway

    def _record(self, method: str, **arguments) -> None:
        self.calls.append((method, arguments))
        if method in self.failing:
            raise self.failing[method]

    async def add_organization(self, org_id: str, name: str) -> None:
        if self.created_anyway:
            self.organizations.add(org_id)
        self._record("add_organization", org_id=org_id, name=name)
        self.organizations.add(org_id)

    async def has_organization(self, org_id: str) -> bool:
        self._record("has_organization", org_id=org_id)
        return org_id in self.organizations

    async def effective_login_policy(self, org_id: str) -> dict:
        self._record("effective_login_policy", org_id=org_id)
        return dict(self.policy)

    async def add_login_policy(self, org_id: str, policy: dict) -> None:
        self._record("add_login_policy", org_id=org_id, policy=policy)

    async def project_role_keys(self, project_id: str) -> list[str]:
        self._record("project_role_keys", project_id=project_id)
        return ["admin", "member"]

    async def grant_project(self, project_id: str, org_id: str, role_keys: list[str]) -> None:
        self._record("grant_project", project_id=project_id, org_id=org_id, role_keys=role_keys)

    async def add_human_user(self, **arguments) -> None:
        made = ZitadelUser(state=ACTIVE, organization_id=arguments["org_id"])
        if self.created_anyway:
            self.users[arguments["user_id"]] = made
        self._record("add_human_user", **arguments)
        self.users[arguments["user_id"]] = made

    async def find_user(self, user_id: str) -> ZitadelUser | None:
        self._record("find_user", user_id=user_id)
        return self.users.get(user_id)

    async def deactivate_user(self, user_id: str) -> None:
        self._record("deactivate_user", user_id=user_id)

    async def reactivate_user(self, user_id: str) -> None:
        self._record("reactivate_user", user_id=user_id)

    async def delete_user(self, user_id: str) -> bool:
        self._record("delete_user", user_id=user_id)
        return self.users.pop(user_id, None) is not None


def _user(state: str = ACTIVE, org_id: str = ORG_ID) -> ZitadelUser:
    return ZitadelUser(state=state, organization_id=org_id)


class FakeLinks:
    """The canonical side: the ``zitadel`` provider row, tenant organizations and user subjects."""

    def __init__(
        self,
        *,
        provider_id: uuid.UUID | None = PROVIDER_ID,
        organizations: dict[uuid.UUID, str] | None = None,
        subjects: dict[uuid.UUID, str] | None = None,
        unfollowed: set[uuid.UUID] | None = None,
        memberships: dict[uuid.UUID, set[uuid.UUID]] | None = None,
    ) -> None:
        self._provider_id = provider_id
        self.organizations = organizations or {}
        self.subjects = subjects or {}
        # Users with a zitadel link the adapter does not follow (admin-made or origin-less).
        self.unfollowed = unfollowed or set()
        # user id -> the tenants it holds a role in.
        self.memberships = memberships or {}
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

    async def has_link(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> bool:
        assert provider_id == self._provider_id
        return user_id in self.subjects or user_id in self.unfollowed

    async def organizations_of_user(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> frozenset[str]:
        assert provider_id == self._provider_id
        tenants = self.memberships.get(user_id, set())
        return frozenset(org for tenant, org in self.organizations.items() if tenant in tenants)


def _member_links(**overrides) -> FakeLinks:
    """USER_ID holds a role in TENANT_ID, whose Zitadel organization is ORG_ID."""
    return FakeLinks(**({"organizations": {TENANT_ID: ORG_ID}, "memberships": {USER_ID: {TENANT_ID}}} | overrides))


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


def _status_change(status: str) -> dict:
    """What UserService sends on activate and deactivate."""
    return {"email": "ada@initech.test", "status": status, STATUS_CHANGED: True}


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
            ("add_organization", {"org_id": ORG_ID, "name": "Initech"}),
            ("effective_login_policy", {"org_id": ORG_ID}),
            ("add_login_policy", {"org_id": ORG_ID, "policy": OWN_POLICY | {"allowRegister": False}}),
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
        adapter, opener, links = _adapter(links=FakeLinks(organizations={TENANT_ID: OTHER_ORG_ID}))

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_ok()
        assert opener.opened == 0
        assert links.writes == []

    async def test_a_tenant_with_no_name_is_refused_before_any_call(self):
        adapter, opener, _ = _adapter()

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": ""})

        assert result.is_error()
        assert opener.opened == 0

    @pytest.mark.parametrize("failure", [_refusal(409), _lost()], ids=["refused", "unanswered"])
    async def test_an_organization_zitadel_does_not_have_after_a_failed_create_links_nothing(self, failure):
        """A name another organization holds is a 409, and the tenant's id is not in Zitadel."""
        api = FakeApi(failing={"add_organization": failure})
        api.organizations.discard(ORG_ID)
        adapter, _, links = _adapter(api)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_error()
        assert [method for method, _ in api.calls] == ["add_organization", "has_organization"]
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

    async def test_a_second_sync_after_a_failure_part_way_finishes_the_organization_and_links_it(self):
        """The organization's id is the tenant's, so a retry finds it instead of making another one."""
        api = FakeApi(failing={"grant_project": _refusal(503)})
        adapter, _, links = _adapter(api)
        assert (await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})).is_error()
        # The first try left the organization with its own policy, and no grant.
        api.failing = {"add_organization": _refusal(409)}
        api.policy = OWN_POLICY | {"allowRegister": False}
        api.calls = []

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_ok(), result
        assert api.calls == [
            ("add_organization", {"org_id": ORG_ID, "name": "Initech"}),
            ("has_organization", {"org_id": ORG_ID}),
            ("effective_login_policy", {"org_id": ORG_ID}),
            ("project_role_keys", {"project_id": PROJECT_ID}),
            ("grant_project", {"project_id": PROJECT_ID, "org_id": ORG_ID, "role_keys": ["admin", "member"]}),
        ]
        assert links.writes == [("organization", TENANT_ID, PROVIDER_ID, ORG_ID)]

    async def test_a_create_whose_answer_was_lost_goes_on_with_the_organization(self):
        api = FakeApi(failing={"add_organization": _lost()}, created_anyway=True)
        adapter, _, links = _adapter(api)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_ok(), result
        assert links.writes == [("organization", TENANT_ID, PROVIDER_ID, ORG_ID)]

    async def test_an_own_policy_that_allows_registration_is_refused_and_links_nothing(self):
        api = FakeApi(policy=OWN_POLICY | {"allowRegister": True})
        adapter, _, links = _adapter(api)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_error()
        assert "registration" in result.error.message
        assert "grant_project" not in dict(api.calls)
        assert links.writes == []

    async def test_a_default_policy_that_links_identity_providers_is_refused_and_links_nothing(self):
        """Its IdPs would not carry over to the organization's own policy, so login through them would stop."""
        api = FakeApi(policy=DEFAULT_POLICY | {"idps": [{"idpId": "400000000000000001", "idpName": "Google"}]})
        adapter, _, links = _adapter(api)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.is_error()
        assert "identity providers" in result.error.message
        assert "add_login_policy" not in dict(api.calls)
        assert links.writes == []


class TestSyncUserCreate:
    async def test_creates_a_human_user_in_the_tenant_organization_and_links_it(self):
        api = FakeApi()
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_ok()
        assert api.calls == [
            ("find_user", {"user_id": str(USER_ID)}),
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
            ("find_user", {"user_id": str(USER_ID)}),
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
        assert [method for method, _ in api.calls] == ["find_user", "add_human_user", "find_user", "deactivate_user"]

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

    async def test_a_user_zitadel_refuses_and_does_not_have_is_not_linked(self):
        api = FakeApi(failing={"add_human_user": _refusal(400)})
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_error()
        assert result.error.context == {"user_id": str(USER_ID), "status": 400}
        assert links.writes == []

    @pytest.mark.parametrize("failure", [_refusal(400), _lost()], ids=["already-existing", "answer-lost"])
    async def test_a_create_that_failed_but_made_the_user_still_links_it(self, failure):
        """Zitadel made the user (answer lost, or it was made by an earlier try): the link is written anyway."""
        api = FakeApi(failing={"add_human_user": failure}, created_anyway=True)
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_ok(), result
        assert links.writes == [("subject", USER_ID, PROVIDER_ID, str(USER_ID), "ada@initech.test")]

    async def test_a_retry_after_the_link_write_failed_links_the_user_zitadel_has_without_creating_it_again(self):
        api = FakeApi(users={str(USER_ID): _user()})
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_ok(), result
        assert api.calls == [("find_user", {"user_id": str(USER_ID)})]
        assert links.writes == [("subject", USER_ID, PROVIDER_ID, str(USER_ID), "ada@initech.test")]

    async def test_a_user_with_this_id_in_another_organization_is_refused_and_not_linked(self):
        api = FakeApi(users={str(USER_ID): _user(org_id=OTHER_ORG_ID)})
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_error()
        assert links.writes == []

    @pytest.mark.parametrize("missing", ["email", "user_name"])
    async def test_a_user_without_email_or_user_name_is_refused_before_any_call(self, missing):
        adapter, opener, _ = _adapter(links=FakeLinks(organizations={TENANT_ID: ORG_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created(**{missing: ""}))

        assert result.is_error()
        assert opener.opened == 0

    async def test_a_linked_user_is_not_created_again_and_its_status_is_not_pushed(self):
        api = FakeApi(users={SUB: _user(INACTIVE)})
        adapter, opener, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}, subjects={USER_ID: SUB}))

        result = await adapter.sync_user(user_id=USER_ID, data=_created())

        assert result.is_ok()
        assert opener.opened == 0
        assert api.calls == []
        assert links.writes == []


class TestSyncUserStatus:
    @pytest.mark.parametrize(
        ("status", "state", "expected"),
        [
            ("inactive", ACTIVE, [("find_user", {"user_id": SUB}), ("deactivate_user", {"user_id": SUB})]),
            ("inactive", LOCKED, [("find_user", {"user_id": SUB}), ("deactivate_user", {"user_id": SUB})]),
            ("inactive", INITIAL, [("find_user", {"user_id": SUB}), ("deactivate_user", {"user_id": SUB})]),
            ("inactive", INACTIVE, [("find_user", {"user_id": SUB})]),
            ("active", INACTIVE, [("find_user", {"user_id": SUB}), ("reactivate_user", {"user_id": SUB})]),
            ("provisioned", INACTIVE, [("find_user", {"user_id": SUB}), ("reactivate_user", {"user_id": SUB})]),
            ("active", ACTIVE, [("find_user", {"user_id": SUB})]),
            ("active", LOCKED, [("find_user", {"user_id": SUB})]),
        ],
    )
    async def test_the_linked_zitadel_user_follows_the_canonical_status(self, status, state, expected):
        api = FakeApi(users={SUB: _user(state)})
        adapter, _, links = _adapter(api, FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.sync_user(
            user_id=USER_ID, data={"email": "ada@initech.test", "status": status, STATUS_CHANGED: True}
        )

        assert result.is_ok()
        assert api.calls == expected
        assert links.writes == []

    async def test_a_deactivation_zitadel_refuses_is_an_error_naming_the_state(self):
        api = FakeApi(users={SUB: _user(INITIAL)}, failing={"deactivate_user": _refusal(400)})
        adapter, _, _ = _adapter(api, FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.sync_user(user_id=USER_ID, data=_status_change("inactive"))

        assert result.is_error()
        assert INITIAL in result.error.message
        assert result.error.context == {"user_id": str(USER_ID), "status": 400, "state": INITIAL}

    async def test_a_linked_user_zitadel_no_longer_has_is_an_error(self):
        adapter, _, _ = _adapter(FakeApi(), FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.sync_user(user_id=USER_ID, data=_status_change("inactive"))

        assert result.is_error()
        assert result.error.context == {"user_id": str(USER_ID)}

    async def test_an_unlinked_user_zitadel_does_not_have_is_an_error(self):
        api = FakeApi()
        adapter, _, links = _adapter(api)

        result = await adapter.sync_user(user_id=USER_ID, data=_status_change("inactive"))

        assert result.is_error()
        assert result.error.operation == "sync_user"
        assert api.calls == [("find_user", {"user_id": str(USER_ID)})]
        assert links.writes == []

    async def test_an_unlinked_user_the_adapter_created_gets_the_status_and_the_link(self):
        """A create whose link write failed: the Zitadel user has the canonical id, so it is found and linked."""
        api = FakeApi(users={str(USER_ID): _user(ACTIVE)})
        adapter, _, links = _adapter(api, _member_links())

        result = await adapter.sync_user(user_id=USER_ID, data=_status_change("inactive"))

        assert result.is_ok(), result
        assert api.calls == [
            ("find_user", {"user_id": str(USER_ID)}),
            ("deactivate_user", {"user_id": str(USER_ID)}),
        ]
        assert links.writes == [("subject", USER_ID, PROVIDER_ID, str(USER_ID), "ada@initech.test")]

    @pytest.mark.parametrize("status", ["inactive", "active"])
    @pytest.mark.parametrize(
        "memberships",
        [{}, {USER_ID: {uuid.UUID("00000000-0000-4000-8000-0000000000dd")}}],
        ids=["no-membership", "member-of-another-tenant"],
    )
    async def test_a_user_with_this_id_outside_the_user_tenants_is_not_changed_and_not_linked(
        self, status, memberships
    ):
        """Zitadel takes a caller-chosen id: a user planted with the canonical id in a foreign org is left alone."""
        api = FakeApi(users={str(USER_ID): _user(INACTIVE if status == "active" else ACTIVE, OTHER_ORG_ID)})
        adapter, _, links = _adapter(api, FakeLinks(organizations={TENANT_ID: ORG_ID}, memberships=memberships))

        result = await adapter.sync_user(user_id=USER_ID, data=_status_change(status))

        assert result.is_error()
        assert result.error.context == {"user_id": str(USER_ID), "organization_id": OTHER_ORG_ID}
        assert api.calls == [("find_user", {"user_id": str(USER_ID)})]
        assert links.writes == []

    @pytest.mark.parametrize("data", [_created(), _status_change("inactive")], ids=["create", "status-change"])
    async def test_a_user_with_a_zitadel_link_the_adapter_does_not_follow_is_refused_before_any_call(self, data):
        """An admin-made or origin-less link: a backend link would collide with it (one link per provider)."""
        api = FakeApi(users={str(USER_ID): _user()})
        adapter, opener, links = _adapter(api, _member_links(unfollowed={USER_ID}))

        result = await adapter.sync_user(user_id=USER_ID, data=data)

        assert result.is_error()
        assert result.error.context == {"user_id": str(USER_ID)}
        assert opener.opened == 0
        assert links.writes == []

    async def test_an_unknown_status_is_refused_before_any_call(self):
        adapter, opener, _ = _adapter(links=FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.sync_user(user_id=USER_ID, data=_status_change("removed"))

        assert result.is_error()
        assert opener.opened == 0


class TestSyncUserWithoutAStatusChange:
    """A profile update or a tenant left (UserService) sends no STATUS_CHANGED: nothing reaches Zitadel.

    An enable or disable made in Zitadel is not undone by a canonical profile edit, and
    a membership is canonical, so leaving a tenant changes nothing in Zitadel.
    """

    @pytest.mark.parametrize(
        "data",
        [
            {"email": "ada@initech.test", "status": "active"},
            {"email": "ada@initech.test", "status": "inactive"},
            {"email": "ada@initech.test", "status": "active", STATUS_CHANGED: False},
            {"email": "ada@initech.test", "status": "active", "left_tenant_id": str(TENANT_ID)},
        ],
        ids=["profile-active", "profile-inactive", "status-changed-false", "left-tenant"],
    )
    @pytest.mark.parametrize("subjects", [{USER_ID: SUB}, {}], ids=["linked", "unlinked"])
    async def test_returns_ok_and_calls_nothing(self, data, subjects):
        api = FakeApi(users={SUB: _user(INACTIVE)})
        adapter, opener, links = _adapter(api, FakeLinks(subjects=dict(subjects)))

        result = await adapter.sync_user(user_id=USER_ID, data=data)

        assert result.is_ok()
        assert opener.opened == 0
        assert api.calls == []
        assert links.writes == []


class TestDeleteUser:
    async def test_removes_the_linked_zitadel_user(self):
        api = FakeApi(users={SUB: _user()})
        adapter, _, _ = _adapter(api, FakeLinks(subjects={USER_ID: SUB}))

        result = await adapter.delete_user(user_id=USER_ID)

        assert result.is_ok()
        assert api.calls == [("delete_user", {"user_id": SUB})]

    async def test_a_user_zitadel_no_longer_has_counts_as_removed(self):
        adapter, _, _ = _adapter(FakeApi(), FakeLinks(subjects={USER_ID: SUB}))

        assert (await adapter.delete_user(user_id=USER_ID)).is_ok()

    async def test_an_unlinked_user_the_adapter_created_is_removed(self):
        api = FakeApi(users={str(USER_ID): _user()})
        adapter, _, _ = _adapter(api, _member_links())

        result = await adapter.delete_user(user_id=USER_ID)

        assert result.is_ok()
        assert api.calls == [
            ("find_user", {"user_id": str(USER_ID)}),
            ("delete_user", {"user_id": str(USER_ID)}),
        ]

    async def test_an_unlinked_user_zitadel_does_not_have_is_an_error(self):
        api = FakeApi()
        adapter, _, _ = _adapter(api, _member_links())

        result = await adapter.delete_user(user_id=USER_ID)

        assert result.is_error()
        assert api.calls == [("find_user", {"user_id": str(USER_ID)})]

    async def test_a_user_with_this_id_outside_the_user_tenants_is_not_deleted(self):
        api = FakeApi(users={str(USER_ID): _user(org_id=OTHER_ORG_ID)})
        adapter, _, _ = _adapter(api, _member_links())

        result = await adapter.delete_user(user_id=USER_ID)

        assert result.is_error()
        assert result.error.context == {"user_id": str(USER_ID), "organization_id": OTHER_ORG_ID}
        assert api.calls == [("find_user", {"user_id": str(USER_ID)})]
        assert str(USER_ID) in api.users

    async def test_a_user_with_a_zitadel_link_the_adapter_does_not_follow_is_refused_before_any_call(self):
        api = FakeApi(users={str(USER_ID): _user()})
        adapter, opener, _ = _adapter(api, _member_links(unfollowed={USER_ID}))

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
            (
                "delete_role_assignment",
                {
                    "user_id": USER_ID,
                    "tenant_id": TENANT_ID,
                    "role_id": uuid.uuid4(),
                    "data": {LAST_ROLE_IN_TENANT: True},
                },
            ),
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


def test_it_keeps_no_memberships_beside_the_role_assignments():
    """Memberships are canonical: RoleService does not tell it which users lost their last role."""
    assert ZitadelSyncAdapter.ends_memberships is False


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
                {"user_id": USER_ID, "data": _status_change("inactive")},
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
        assert result.error.context[UNANSWERED] is True
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


class TestUnanswered:
    """``context[UNANSWERED]`` marks a failure where Zitadel gave no readable answer, and only that."""

    @staticmethod
    def _adapter_answering(handler) -> ZitadelSyncAdapter:
        @asynccontextmanager
        async def open_api():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                yield ZitadelAdminApi(http, base_url="http://zitadel.localhost:8480", token="local-test-token")

        return ZitadelSyncAdapter(open_api, FakeLinks(), project_id=PROJECT_ID)

    async def test_a_timeout_is_unanswered(self):
        def time_out(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        result = await self._adapter_answering(time_out).sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.error.context == {"tenant_id": str(TENANT_ID), "organization_id": ORG_ID, UNANSWERED: True}

    async def test_an_answer_that_is_not_json_is_unanswered(self):
        def garble(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>proxy</html>")

        result = await self._adapter_answering(garble).sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.error.context == {"tenant_id": str(TENANT_ID), "organization_id": ORG_ID, UNANSWERED: True}

    async def test_a_refusal_is_answered(self):
        api = FakeApi(failing={"add_organization": _refusal(409)})
        adapter, _, _ = _adapter(api)

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.error.context == {"tenant_id": str(TENANT_ID), "status": 409, "organization_id": ORG_ID}

    async def test_a_refusal_before_any_call_is_not_unanswered(self):
        adapter, _, _ = _adapter(links=FakeLinks(provider_id=None))

        result = await adapter.sync_tenant(tenant_id=TENANT_ID, data={"name": "Initech"})

        assert result.error.context == {"tenant_id": str(TENANT_ID)}
