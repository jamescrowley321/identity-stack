"""The member routes for a Keycloak tenant admin, against ``keycloak-test`` and the test Postgres (#677).

A deployment that syncs to Keycloak (``SYNC_PROVIDER=keycloak``) and authorizes
Keycloak principals from the canonical model, as in ``test_keycloak_role_routes``.
The routes, ``get_member_directory``, the services and ``KeycloakSyncAdapter`` are
the shipped wiring over the shipped admin client: nothing is stubbed but the cache
publisher. Each member operation is read back from the realm with the realm
admin's own token. Every account the test makes is removed afterwards whatever the
outcome.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.dependencies import identity, rbac
from app.dependencies.rbac import RoleGrantRefusal, get_rbac_session
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import keycloak_config
from app.middleware.rate_limit import limiter
from app.models.database import get_async_session
from app.models.identity.assignment import UserTenantRole
from app.models.identity.user import User
from app.routers import users
from app.routers.users import SHARED_ACCOUNT_DETAIL
from app.services.keycloak_admin import (
    ADMIN_CLIENT_ID_VARIABLE,
    ADMIN_CREDENTIAL_VARIABLE,
    ADMIN_TIMEOUT_SECONDS,
    API_CLIENT_VARIABLE,
    ISSUER_VARIABLE,
    connect_keycloak_admin,
)
from app.services.user import MemberRefusal
from tests.helpers.keycloak import DAVE, FRANK, KeycloakTokenSource
from tests.helpers.keycloak_model import ADMIN_CLIENT_ID, ADMIN_CLIENT_SECRET, API_AUDIENCE, SPA_CLIENT_ID
from tests.integration.test_keycloak_role_routes import (
    NOT_GRANTED,
    _assign,
    _global_role,
    _linked_user,
    _provider,
    _signed_in,
    _tenant,
)
from tests.integration.test_keycloak_sync_adapter import find_organization, find_user, groups, members


@pytest.fixture
def keycloak_deployment(monkeypatch, keycloak_token_source: KeycloakTokenSource) -> KeycloakTokenSource:
    monkeypatch.setattr(identity, "SYNC_PROVIDER", "keycloak")
    monkeypatch.setattr(rbac, "CANONICAL_RBAC_PROVIDERS", frozenset({"ory", "keycloak"}))
    monkeypatch.setenv(ISSUER_VARIABLE, keycloak_token_source.issuer)
    monkeypatch.setenv(ADMIN_CLIENT_ID_VARIABLE, ADMIN_CLIENT_ID)
    monkeypatch.setenv(ADMIN_CREDENTIAL_VARIABLE, ADMIN_CLIENT_SECRET)
    monkeypatch.setenv(API_CLIENT_VARIABLE, API_AUDIENCE)
    return keycloak_token_source


@pytest.fixture
def realm(keycloak_token_source: KeycloakTokenSource) -> Iterator[httpx.Client]:
    """The realm admin's view of the realm, to read back what the routes pushed."""
    with keycloak_token_source._admin() as admin:
        yield admin


@pytest_asyncio.fixture(loop_scope="session")
async def client(keycloak_deployment, db_session) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.state.limiter = limiter
    app.state.cache_publisher = AsyncMock()
    app.include_router(users.router, prefix="/api")

    async def _session():
        yield db_session

    app.dependency_overrides[get_rbac_session] = _session
    app.dependency_overrides[get_async_session] = _session
    keycloak = keycloak_config(keycloak_deployment.issuer, API_AUDIENCE, SPA_CLIENT_ID, require_https=False)
    app.add_middleware(TokenValidationMiddleware, providers=[keycloak])
    limiter.reset()
    async with httpx.AsyncClient(timeout=ADMIN_TIMEOUT_SECONDS) as http:
        app.state.keycloak_admin = await connect_keycloak_admin(http)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            yield api


@pytest.fixture
def invited(realm: httpx.Client) -> Iterator[list[str]]:
    """Usernames of the accounts the invitations create, each deleted afterwards."""
    usernames: list[str] = []
    yield usernames
    for username in usernames:
        found = realm.get("/users", params={"username": username, "exact": "true"})
        for account in found.json() if found.is_success else []:
            realm.delete(f"/users/{account['id']}")


@pytest.fixture
def account(realm: httpx.Client) -> Iterator[dict]:
    """A realm account of a user no tenant has yet, removed afterwards."""
    username = f"member-{uuid.uuid4().hex[:12]}"
    created = realm.post("/users", json={"username": username, "email": f"{username}@example.com", "enabled": True})
    created.raise_for_status()
    account_id = created.headers["location"].rsplit("/", 1)[1]
    yield realm.get(f"/users/{account_id}").json()
    realm.delete(f"/users/{account_id}")


class TestKeycloakTenantAdminsManageMembers:
    """``dave`` administers acme alone: each member operation reaches the acme organization."""

    async def test_invite_list_deactivate_activate_and_remove_each_reach_keycloak(
        self,
        keycloak_deployment,
        client,
        realm,
        db_session,
        invited,
    ):
        provider = await _provider(db_session, keycloak_deployment.issuer)
        acme = await _tenant(db_session, provider, "acme")
        admin = await _signed_in(db_session, provider, keycloak_deployment, DAVE, acme, "admin")
        await _global_role(db_session, "member")
        organization = find_organization(realm, "acme")
        email = f"invited-{uuid.uuid4().hex[:12]}@example.com"

        sent = await client.post("/api/members/invite", json={"email": email, "role_names": ["member"]}, headers=admin)
        assert sent.status_code == 200, sent.text
        canonical = (await db_session.execute(select(User).where(User.email == email))).scalar_one()
        user_id = str(canonical.id)
        invited.append(user_id)
        # The answer names the new member and nothing else of the account.
        assert sent.json() == {
            "status": "invited",
            "email": email,
            "user": {"userId": user_id, "email": email, "roleNames": ["member"]},
        }
        # The account is created under the canonical id, enabled, a member of acme and of its member group.
        account = find_user(realm, canonical.id)
        assert (account["email"], account["enabled"]) == (email, True)
        assert user_id in members(realm, organization)
        assert user_id in groups(realm, organization)["member"]["members"]

        listed = await client.get("/api/members", headers=admin)
        assert listed.status_code == 200, listed.text
        assert {member["userId"]: member for member in listed.json()["members"]}[user_id] == {
            "userId": user_id,
            "email": email,
            "name": email,
            "status": "enabled",
            "roleNames": ["member"],
        }

        again = await client.post("/api/members/invite", json={"email": email}, headers=admin)
        assert (again.status_code, again.json()) == (409, {"detail": MemberRefusal.ACCOUNT_EXISTS})

        deactivated = await client.post(f"/api/members/{user_id}/deactivate", headers=admin)
        assert (deactivated.status_code, deactivated.json()) == (200, {"status": "deactivated", "user_id": user_id})
        assert find_user(realm, canonical.id)["enabled"] is False

        activated = await client.post(f"/api/members/{user_id}/activate", headers=admin)
        assert (activated.status_code, activated.json()) == (200, {"status": "activated", "user_id": user_id})
        assert find_user(realm, canonical.id)["enabled"] is True

        removed = await client.delete(f"/api/members/{user_id}", headers=admin)
        assert (removed.status_code, removed.json()) == (200, {"status": "removed", "user_id": user_id})
        assert user_id not in members(realm, organization)
        relisted = await client.get("/api/members", headers=admin)
        assert user_id not in {member["userId"] for member in relisted.json()["members"]}

    async def test_inviting_another_tenants_account_is_refused_and_enrols_nothing(
        self,
        keycloak_deployment,
        client,
        realm,
        db_session,
        account,
    ):
        """An invitation only creates: globex's account is neither enrolled in acme nor described to its admin."""
        provider = await _provider(db_session, keycloak_deployment.issuer)
        acme = await _tenant(db_session, provider, "acme")
        globex = await _tenant(db_session, provider, "globex")
        admin = await _signed_in(db_session, provider, keycloak_deployment, DAVE, acme, "admin")
        existing = await _linked_user(db_session, provider, account["id"], account["email"], account["username"])
        await _assign(db_session, existing, globex, "member")
        await _global_role(db_session, "viewer")
        organization = find_organization(realm, "acme")

        sent = await client.post(
            "/api/members/invite", json={"email": account["email"], "role_names": ["viewer"]}, headers=admin
        )

        assert (sent.status_code, sent.json()) == (409, {"detail": MemberRefusal.ACCOUNT_EXISTS})
        held = (await db_session.execute(select(UserTenantRole).where(UserTenantRole.user_id == existing.id))).scalars()
        assert {row.tenant_id for row in held} == {globex.id}
        assert account["username"] not in members(realm, organization)
        assert find_user(realm, existing.id) is None

    async def test_a_member_who_also_belongs_to_another_tenant_keeps_its_status(
        self,
        keycloak_deployment,
        client,
        realm,
        db_session,
        account,
    ):
        """The status is the account's: acme alone does not decide it while globex holds the account too."""
        provider = await _provider(db_session, keycloak_deployment.issuer)
        acme = await _tenant(db_session, provider, "acme")
        globex = await _tenant(db_session, provider, "globex")
        admin = await _signed_in(db_session, provider, keycloak_deployment, DAVE, acme, "admin")
        shared = await _linked_user(db_session, provider, account["id"], account["email"], account["username"])
        await _assign(db_session, shared, acme, "member")
        await _assign(db_session, shared, globex, "member")

        refused = await client.post(f"/api/members/{shared.id}/deactivate", headers=admin)

        assert (refused.status_code, refused.json()) == (409, {"detail": SHARED_ACCOUNT_DETAIL})
        assert realm.get(f"/users/{account['id']}").json()["enabled"] is True

    async def test_a_member_of_the_tenant_is_refused_every_member_route(
        self,
        keycloak_deployment,
        client,
        realm,
        db_session,
        account,
    ):
        provider = await _provider(db_session, keycloak_deployment.issuer)
        acme = await _tenant(db_session, provider, "acme")
        member = await _signed_in(db_session, provider, keycloak_deployment, FRANK, acme, "member")
        target = await _linked_user(db_session, provider, account["id"], account["email"], account["username"])
        await _assign(db_session, target, acme, "member")
        email = f"refused-{uuid.uuid4().hex[:12]}@example.com"

        answers = {
            "list": await client.get("/api/members", headers=member),
            "invite": await client.post("/api/members/invite", json={"email": email}, headers=member),
            "deactivate": await client.post(f"/api/members/{target.id}/deactivate", headers=member),
            "activate": await client.post(f"/api/members/{target.id}/activate", headers=member),
            "remove": await client.delete(f"/api/members/{target.id}", headers=member),
        }

        assert {what: (answer.status_code, answer.json()) for what, answer in answers.items()} == dict.fromkeys(
            answers, (403, NOT_GRANTED)
        )
        assert realm.get("/users", params={"email": email, "exact": "true"}).json() == []
        assert realm.get(f"/users/{account['id']}").json()["enabled"] is True

    async def test_an_admin_of_its_sole_tenant_cannot_invite_an_operator(
        self, keycloak_deployment, client, realm, db_session
    ):
        """``operator`` opens every tenant's state: ``dave`` administers acme alone and is no operator."""
        provider = await _provider(db_session, keycloak_deployment.issuer)
        acme = await _tenant(db_session, provider, "acme")
        admin = await _signed_in(db_session, provider, keycloak_deployment, DAVE, acme, "admin")
        await _global_role(db_session, "operator")
        email = f"operator-{uuid.uuid4().hex[:12]}@example.com"

        refused = await client.post(
            "/api/members/invite", json={"email": email, "role_names": ["operator"]}, headers=admin
        )

        assert (refused.status_code, refused.json()) == (403, {"detail": RoleGrantRefusal.OPERATOR_GRANT})
        assert (await db_session.execute(select(User).where(User.email == email))).scalar_one_or_none() is None
        assert realm.get("/users", params={"email": email, "exact": "true"}).json() == []
