"""Tenant resolution for a canonical provider, against a real token and a real database.

A provider-signed Ory JWT goes through the shipped middleware, and the tenant
and the roles come from the canonical rows in Postgres through the shipped
``get_tenant_id`` and ``require_role`` (conventions §18). Nothing is stubbed: the
unit suite covers the branches, and this proves the one path that needs a
signature and SQL.
"""

import uuid

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.dependencies.identity import get_identity_provisioning_service, get_identity_resolution_service
from app.dependencies.rbac import get_rbac_session, require_role
from app.dependencies.tenant import get_tenant_id
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import descope_config, ory_config
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.routers.protected import canonical_identity
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService

TENANT_ROUTE = "/api/probe/tenant"
ADMIN_ROUTE = "/api/probe/admin"
NO_TENANT_FROM_GET_TENANT_ID = {"detail": "No tenant context — select a tenant first"}


def _build_app(db_session, *, issuer: str, audience: str) -> FastAPI:
    """The shipped middleware and dependencies on the test transaction."""
    app = FastAPI()
    app.add_api_route("/api/identity", canonical_identity, methods=["GET"])

    @app.get(TENANT_ROUTE)
    async def tenant(tenant_id: str = Depends(get_tenant_id)):
        return {"tenant_id": tenant_id}

    @app.get(ADMIN_ROUTE)
    async def admin(tenant_id: str = Depends(get_tenant_id), roles: list[str] = Depends(require_role("admin"))):
        return {"tenant_id": tenant_id, "roles": sorted(roles)}

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

    app.dependency_overrides[get_identity_resolution_service] = _resolver
    app.dependency_overrides[get_identity_provisioning_service] = _provisioner
    app.dependency_overrides[get_rbac_session] = _session
    app.add_middleware(
        TokenValidationMiddleware,
        providers=[descope_config(""), ory_config(issuer, audience)],
    )
    return app


async def _provisioned_ory_user(db_session, client: AsyncClient, ory_token_source, prefix: str) -> tuple[dict, str]:
    """Log an Ory user in once so JIT provisioning puts them in one tenant.

    Returns the request headers carrying their token, and that tenant's id.
    """
    db_session.add(Provider(name="ory", type=ProviderType.ory, issuer_url=ory_token_source.issuer))
    await db_session.flush()
    sub = f"{prefix}-{uuid.uuid4()}"
    email = f"{sub}@example.com"
    headers = {"Authorization": f"Bearer {ory_token_source.mint(sub=sub, email=email, email_verified=True)}"}

    identity = await client.get("/api/identity", headers=headers)
    assert identity.status_code == 200, identity.text
    (membership,) = identity.json()["tenant_memberships"]
    return headers, membership["tenant_id"]


async def _add_membership(db_session, *, user_email: str, role_name: str) -> str:
    """Put the user in a second, new tenant with ``role_name``; returns the tenant id."""
    user = await UserRepository(db_session).get_by_email(user_email)
    assert user is not None, f"expected a canonical user for {user_email}"
    tenant = Tenant(name=f"second-tenant-{uuid.uuid4()}")
    db_session.add(tenant)
    await db_session.flush()
    role = Role(name=role_name, description="granted by the tenant-resolution test", tenant_id=tenant.id)
    db_session.add(role)
    await db_session.flush()
    db_session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
    await db_session.flush()
    return str(tenant.id)


async def _email_of(client: AsyncClient, headers: dict) -> str:
    return (await client.get("/api/identity", headers=headers)).json()["user"]["email"]


@pytest.mark.asyncio
async def test_ory_principal_with_one_membership_gets_that_tenant(db_session, ory_token_source):
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, tenant_id = await _provisioned_ory_user(db_session, client, ory_token_source, "tenant-sole")

        response = await client.get(TENANT_ROUTE, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"tenant_id": tenant_id}


@pytest.mark.asyncio
async def test_ory_principal_in_two_tenants_selects_one_with_the_header(db_session, ory_token_source):
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, first_tenant = await _provisioned_ory_user(db_session, client, ory_token_source, "tenant-two")
        second_tenant = await _add_membership(
            db_session, user_email=await _email_of(client, headers), role_name="admin"
        )

        unselected = await client.get(TENANT_ROUTE, headers=headers)
        in_first = await client.get(TENANT_ROUTE, headers={**headers, TENANT_SELECTOR_HEADER: first_tenant})
        admin_in_first = await client.get(ADMIN_ROUTE, headers={**headers, TENANT_SELECTOR_HEADER: first_tenant})
        admin_in_second = await client.get(ADMIN_ROUTE, headers={**headers, TENANT_SELECTOR_HEADER: second_tenant})

    assert unselected.status_code == 403
    assert unselected.json() == NO_TENANT_FROM_GET_TENANT_ID
    assert in_first.json() == {"tenant_id": first_tenant}
    # The admin role is held in the second tenant only, and does not follow the
    # caller into the first.
    assert admin_in_first.status_code == 403
    assert admin_in_first.json() == {"detail": "Insufficient permissions"}
    assert admin_in_second.status_code == 200, admin_in_second.text
    assert admin_in_second.json() == {"tenant_id": second_tenant, "roles": ["admin"]}


@pytest.mark.asyncio
async def test_selecting_a_tenant_the_caller_is_not_in_is_refused(db_session, ory_token_source):
    """The other tenant exists and has an admin role, and none of it is granted."""
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _provisioned_ory_user(db_session, client, ory_token_source, "tenant-foreign")
        foreign = Tenant(name=f"foreign-tenant-{uuid.uuid4()}")
        db_session.add(foreign)
        await db_session.flush()
        db_session.add(Role(name="admin", description="held by nobody in this test", tenant_id=foreign.id))
        await db_session.flush()
        selected = {**headers, TENANT_SELECTOR_HEADER: str(foreign.id)}

        tenant = await client.get(TENANT_ROUTE, headers=selected)
        admin = await client.get(ADMIN_ROUTE, headers=selected)

    assert tenant.status_code == 403
    assert tenant.json() == NO_TENANT_FROM_GET_TENANT_ID
    assert admin.status_code == 403
    assert admin.json() == NO_TENANT_FROM_GET_TENANT_ID
