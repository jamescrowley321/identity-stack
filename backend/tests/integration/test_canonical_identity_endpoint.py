"""Integration test for GET /api/identity — resolve + JIT provisioning over HTTP."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from py_identity_model import to_principal

from app.dependencies.identity import (
    get_identity_provisioning_service,
    get_identity_resolution_service,
)
from app.models.identity.provider import Provider, ProviderType
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.routers.protected import canonical_identity
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService


def _build_app(db_session, claims: dict, auth_type: str) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        request.state.claims = claims
        request.state.principal = to_principal(claims, auth_type)
        return await call_next(request)

    app.add_api_route("/api/identity", canonical_identity, methods=["GET"])

    async def _resolver() -> IdentityResolutionService:
        return IdentityResolutionService(
            user_repository=UserRepository(db_session),
            idp_link_repository=IdPLinkRepository(db_session),
            provider_repository=ProviderRepository(db_session),
            assignment_repository=UserTenantRoleRepository(db_session),
            role_repository=RoleRepository(db_session),
            tenant_repository=TenantRepository(db_session),
            redis_client=None,
        )

    async def _provisioner() -> IdentityProvisioningService:
        return IdentityProvisioningService(
            session=db_session,
            provider_repository=ProviderRepository(db_session),
            idp_link_repository=IdPLinkRepository(db_session),
            user_repository=UserRepository(db_session),
            tenant_repository=TenantRepository(db_session),
            role_repository=RoleRepository(db_session),
            assignment_repository=UserTenantRoleRepository(db_session),
        )

    app.dependency_overrides[get_identity_resolution_service] = _resolver
    app.dependency_overrides[get_identity_provisioning_service] = _provisioner
    return app


async def _seed_ory(db_session):
    db_session.add(Provider(name="ory", type=ProviderType.ory, issuer_url="https://x.projects.oryapis.com"))
    await db_session.flush()


@pytest.mark.asyncio
async def test_first_login_jit_provisions_and_returns_canonical_identity(db_session):
    await _seed_ory(db_session)
    claims = {
        "sub": "ory-http-1",
        "email": "http@example.com",
        "email_verified": True,
        "given_name": "Http",
        "family_name": "User",
    }
    app = _build_app(db_session, claims, auth_type="Ory")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/identity")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["email"] == "http@example.com"
    assert body["user"]["given_name"] == "Http"
    role_names = {r["role_name"] for r in body["roles"]}
    assert "member" in role_names
    assert any(idp["provider_name"] == "ory" for idp in body["linked_idps"])

    # Second call resolves the same user (idempotent — no duplicate provisioning).
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp2 = await c.get("/api/identity")
    assert resp2.status_code == 200
    assert resp2.json()["user"]["id"] == body["user"]["id"]


@pytest.mark.asyncio
async def test_non_jit_provider_without_link_returns_404(db_session):
    # Descope is not JIT-enabled: an unlinked subject is not auto-provisioned.
    db_session.add(Provider(name="descope", type=ProviderType.descope, issuer_url="https://api.descope.com"))
    await db_session.flush()
    claims = {"sub": "descope-unlinked", "email": "d@example.com"}
    app = _build_app(db_session, claims, auth_type="Descope")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/identity")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_missing_subject_returns_401(db_session):
    await _seed_ory(db_session)
    app = _build_app(db_session, {"email": "no-sub@example.com"}, auth_type="Ory")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/identity")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unverified_email_cannot_take_over_existing_user_via_endpoint(db_session):
    """A token asserting a victim's email WITHOUT email_verified must fail closed
    (400) and must not link the attacker's Ory subject to the victim."""
    from app.models.identity.user import User, UserStatus

    await _seed_ory(db_session)
    victim = User(email="victim@corp.com", user_name="victim@corp.com", status=UserStatus.active)
    db_session.add(victim)
    await db_session.flush()

    claims = {"sub": "ory|attacker", "email": "victim@corp.com"}  # no email_verified
    app = _build_app(db_session, claims, auth_type="Ory")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/identity")
    assert resp.status_code == 400
    assert await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", "ory|attacker") is None
