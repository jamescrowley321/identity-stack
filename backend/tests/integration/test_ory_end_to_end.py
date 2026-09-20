"""End-to-end validation of the Ory provider path (FR-21).

This is the capstone for the "Ory as a configurable SSO provider" work. Every
other Ory test in the suite patches ``validate_token`` or injects claims
directly; this one drives a **real, provider-signed JWT** through the shipped
middleware and asserts the whole chain:

    real Ory token -> signature verified against the provider's JWKS
                   -> issuer allow-list -> audience
                   -> JIT provisioning of a canonical user
                   -> GET /api/identity payload
                   -> an RBAC-gated route

No validation logic is added anywhere for this (NFR-4): the middleware calls
py-identity-model's ``validate_token`` exactly as it does in production, and the
token comes from a real authorization-code exchange. The provider is the local
``hydra-test`` container by default and a live Ory project when the ``ORY_TEST_*``
vars are set — see ``ory_token_source`` in conftest. Ory Network's OAuth2 surface
is Ory Hydra, so both targets exercise the same code.

The Descope path is untouched: ``test_descope_token_is_not_accepted_on_the_ory_provider``
asserts the two providers stay separated, and the rest of the integration suite
covers Descope as before (NFR-8).
"""

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import (
    get_identity_provisioning_service,
    get_identity_resolution_service,
    get_sync_status_service,
)
from app.dependencies.rbac import get_rbac_session
from app.middleware.auth import TokenValidationMiddleware
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.sync_event import SyncEventRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.routers import sync_status
from app.routers.protected import canonical_identity
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService
from app.services.sync_status import SyncStatusService

# An RBAC-gated route that needs no Descope `dct` of its own, so it isolates the
# thing under test: whether a principal carrying no authorization claims can be
# authorized at all.
RBAC_GATED_ROUTE = "/api/sync/status"
RBAC_GATED_ROLE = "operator"


def _build_app(db_session, *, issuer: str, audience: str) -> FastAPI:
    """An app wired the way ``app.main`` wires the Ory path, on the test session.

    Built here rather than imported from ``app.main`` because main builds its
    provider list at import time, before a test can set ``ORY_ISSUER_URL``.
    Everything inside the app is the shipped object: the real middleware, the real
    ``GET /api/identity`` handler, the real sync-status router with its real
    ``require_role`` dependency.
    """
    app = FastAPI()

    app.add_api_route("/api/identity", canonical_identity, methods=["GET"])
    app.include_router(sync_status.router, prefix="/api")

    async def _session():
        yield db_session

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

    async def _sync_status_service() -> SyncStatusService:
        return SyncStatusService(
            provider_repository=ProviderRepository(db_session),
            idp_link_repository=IdPLinkRepository(db_session),
            sync_event_repository=SyncEventRepository(db_session),
        )

    app.dependency_overrides[get_identity_resolution_service] = _resolver
    app.dependency_overrides[get_identity_provisioning_service] = _provisioner
    app.dependency_overrides[get_sync_status_service] = _sync_status_service
    # Bind canonical role resolution to the test transaction, so a role granted in
    # this test is the role the authorization path reads.
    app.dependency_overrides[get_rbac_session] = _session

    app.add_middleware(
        TokenValidationMiddleware,
        descope_project_id="",
        ory_issuer_url=issuer,
        ory_audience=audience,
    )
    return app


async def _seed_ory_provider(db_session, issuer: str) -> None:
    db_session.add(Provider(name="ory", type=ProviderType.ory, issuer_url=issuer))
    await db_session.flush()


async def _grant_role(db_session, *, email: str, role_name: str) -> None:
    """Grant ``role_name`` to the canonical user in the tenant JIT put them in."""
    user = await UserRepository(db_session).get_by_email(email)
    assert user is not None, f"expected a canonical user for {email}"
    assignments = await UserTenantRoleRepository(db_session).list_by_user(user.id)
    assert assignments, "expected JIT provisioning to have created a tenant assignment"
    tenant_id = assignments[0].tenant_id

    role = Role(name=role_name, description="granted by the Ory end-to-end test", tenant_id=tenant_id)
    db_session.add(role)
    await db_session.flush()
    db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant_id, role_id=role.id))
    await db_session.flush()


def _unique_subject(prefix: str) -> str:
    """A fresh subject per test — Hydra's store outlives the database rollback."""
    return f"{prefix}-{uuid.uuid4()}"


@pytest.mark.asyncio
async def test_ory_jwt_authenticates_and_jit_provisions_canonical_identity(db_session, ory_token_source):
    """A real Ory JWT authenticates, provisions a canonical user, and resolves."""
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    sub = _unique_subject("ory-e2e")
    email = f"{sub}@example.com"
    token = ory_token_source.mint(
        sub=sub,
        email=email,
        email_verified=True,
        given_name="Ory",
        family_name="Capstone",
    )

    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/identity", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        body = response.json()

        # The canonical payload, not the token's claims.
        assert body["user"]["email"] == email
        assert body["user"]["given_name"] == "Ory"
        assert body["user"]["family_name"] == "Capstone"
        assert {role["role_name"] for role in body["roles"]} == {"member"}
        assert body["tenant_memberships"], "JIT provisioning must place the user in a tenant"
        assert {"provider_name": "ory", "external_sub": sub} in body["linked_idps"]

        # Second call resolves the same user — provisioning is idempotent.
        again = await client.get("/api/identity", headers={"Authorization": f"Bearer {token}"})
        assert again.status_code == 200
        assert again.json()["user"]["id"] == body["user"]["id"]

    assert await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", sub) is not None


@pytest.mark.asyncio
async def test_ory_jwt_reaches_an_rbac_gated_route(db_session, ory_token_source):
    """An Ory principal whose canonical role matches is authorized.

    Ory tokens carry no ``dct``/``tenants`` claims, so this passes only because
    ``require_role`` resolves roles from the canonical model for providers that do
    not put authorization in the token.
    """
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    sub = _unique_subject("ory-rbac-allow")
    email = f"{sub}@example.com"
    token = ory_token_source.mint(sub=sub, email=email, email_verified=True)
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # First login provisions the canonical user (member only) ...
        assert (await client.get("/api/identity", headers=headers)).status_code == 200
        # ... which is not enough for this route.
        assert (await client.get(RBAC_GATED_ROUTE, headers=headers)).status_code == 403

        await _grant_role(db_session, email=email, role_name=RBAC_GATED_ROLE)

        response = await client.get(RBAC_GATED_ROUTE, headers=headers)
        assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_ory_principal_without_canonical_identity_is_rejected_by_rbac_route(db_session, ory_token_source):
    """Authenticated is not authorized: a valid token with no canonical grants 403s."""
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    sub = _unique_subject("ory-rbac-unprovisioned")
    token = ory_token_source.mint(sub=sub, email=f"{sub}@example.com", email_verified=True)
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(RBAC_GATED_ROUTE, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_ory_token_without_an_email_claim_cannot_provision(db_session, ory_token_source):
    """JIT fails closed without a verified email — no user, no link, no 500."""
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    sub = _unique_subject("ory-no-email")
    token = ory_token_source.mint(sub=sub, email=None)
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/identity", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 400
    assert await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", sub) is None


@pytest.mark.asyncio
async def test_ory_token_with_an_unverified_email_cannot_provision(db_session, ory_token_source):
    """``email_verified: false`` is not a verified email — the takeover guard holds
    against a real token, not just an injected claim dict."""
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    sub = _unique_subject("ory-unverified-email")
    token = ory_token_source.mint(sub=sub, email=f"{sub}@example.com", email_verified=False)
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/identity", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 400
    assert await IdPLinkRepository(db_session).get_by_provider_name_and_sub("ory", sub) is None


@pytest.mark.asyncio
async def test_ory_token_for_a_different_audience_is_rejected(db_session, ory_token_source):
    """A token the same Ory project signed, for a different API, must not authenticate.

    This is the confused-deputy case ``require_audience=True`` exists for: the
    signature and issuer are both genuine, so audience is the only thing that binds
    the token to *this* API.
    """
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    token = ory_token_source.mint(sub=_unique_subject("ory-wrong-aud"), email="wrong-aud@example.com")
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience="a-different-relying-party")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/identity", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_tampered_ory_token_is_rejected(db_session, ory_token_source):
    """Flipping one character of the signature must fail verification.

    Proves the signature is actually checked against the provider's JWKS rather
    than the token merely being decoded.
    """
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    token = ory_token_source.mint(sub=_unique_subject("ory-tampered"), email="tampered@example.com")
    header, payload, signature = token.split(".")
    # Mutate the FIRST signature character, not the last. For an RS256 signature
    # (256 bytes, length 1 mod 3) the final base64url character carries two
    # significant bits plus four bits of padding, so a canonical encoder emits only
    # "A", "Q", "g" or "w" there — and swapping "A" for "B" changes padding alone,
    # which the decoder discards. The signature then still verifies and the request
    # returns 200. Character 0 carries six significant bits, so changing it always
    # changes the decoded bytes.
    flipped = "A" if signature[0] != "A" else "B"
    tampered = f"{header}.{payload}.{flipped}{signature[1:]}"
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/identity", headers={"Authorization": f"Bearer {tampered}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_ory_token_is_rejected(db_session, ory_token_source):
    """``exp`` is enforced on the Ory path, not only on the Descope one."""
    import time

    if ory_token_source.is_live:
        pytest.skip(
            "needs a client with a one-second access-token lifespan; the live Ory "
            "client is owner-managed and its lifespans are not test-controlled"
        )
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    client_id, client_secret = ory_token_source.register_client(authorization_code_grant_access_token_lifespan="1s")
    token = ory_token_source.mint(
        sub=_unique_subject("ory-expired"),
        email="expired@example.com",
        client_id=client_id,
        client_secret=client_secret,
    )
    time.sleep(2)

    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/identity", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_descope_token_is_not_accepted_on_the_ory_provider(db_session, ory_token_source, access_token):
    """A genuine Descope token must not authenticate against an Ory-only backend.

    The two providers are separated by the issuer allow-list before any crypto
    runs. Configuring Ory must not make a second identity provider's tokens
    acceptable (NFR-8 cuts both ways).
    """
    await _seed_ory_provider(db_session, ory_token_source.issuer)
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/identity", headers={"Authorization": f"Bearer {access_token}"})
    assert response.status_code == 401
