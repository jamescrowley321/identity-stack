"""Tenant resolution for a canonical provider, against a real token and a real database.

A provider-signed Ory JWT goes through the shipped middleware, and the tenant
and the roles come from the canonical rows in Postgres through the shipped
``get_tenant_id`` and ``require_role`` (conventions §18). Nothing is stubbed: the
unit suite covers the branches, and this proves the one path that needs a
signature and SQL.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.dependencies.identity import get_identity_provisioning_service, get_identity_resolution_service
from app.dependencies.rbac import get_rbac_session, require_role
from app.dependencies.tenant import get_tenant_id
from app.dependencies.tenant_access import TENANT_SELECTOR_HEADER
from app.errors.identity import IdentityErrorRaised
from app.errors.problem_detail import identity_error_raised_handler
from app.middleware.auth import TokenValidationMiddleware
from app.middleware.providers import descope_config, ory_config
from app.middleware.rate_limit import limiter
from app.models.database import get_async_session
from app.models.document import Document
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Permission, Role
from app.models.identity.tenant import Tenant
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.routers import accesskeys, canonical_users, documents, fga, permissions, roles, users
from app.routers.protected import canonical_identity
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService

TENANT_ROUTE = "/api/probe/tenant"
ADMIN_ROUTE = "/api/probe/admin"
OPERATOR_ROUTE = "/api/probe/operator"
NO_TENANT_FROM_GET_TENANT_ID = {"detail": "No tenant context — select a tenant first"}
DESCOPE_PRINCIPALS_ONLY = {"detail": "This route serves Descope principals only"}
# Descope's access keys and FGA store serve Descope principals only (#678): to anyone
# else their routes are not there.
CAPABILITY_MISSING = (404, "/errors/capability-missing")


def _build_app(db_session, *, issuer: str, audience: str) -> FastAPI:
    """The shipped middleware and dependencies on the test transaction."""
    app = FastAPI()
    app.add_api_route("/api/identity", canonical_identity, methods=["GET"])
    # The shipped Descope-backed routers, over a Descope client that records its calls.
    app.state.limiter = limiter
    app.state.descope_client = AsyncMock()
    app.state.cache_publisher = MagicMock()
    routers = (
        documents.router,
        users.router,
        accesskeys.router,
        roles.router,
        permissions.router,
        fga.router,
        canonical_users.router,
    )
    for router in routers:
        app.include_router(router, prefix="/api")
    app.add_exception_handler(IdentityErrorRaised, identity_error_raised_handler)

    @app.get(TENANT_ROUTE)
    async def tenant(tenant_id: str = Depends(get_tenant_id)):
        return {"tenant_id": tenant_id}

    @app.get(ADMIN_ROUTE)
    async def admin(tenant_id: str = Depends(get_tenant_id), roles: list[str] = Depends(require_role("admin"))):
        return {"tenant_id": tenant_id, "roles": sorted(roles)}

    @app.get(OPERATOR_ROUTE)
    async def operator(tenant_id: str = Depends(get_tenant_id), roles: list[str] = Depends(require_role("operator"))):
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
    app.dependency_overrides[get_async_session] = _session
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


async def _document_count(db_session) -> int:
    return (await db_session.execute(select(func.count()).select_from(Document))).scalar_one()


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


@pytest.mark.asyncio
async def test_ory_principal_is_refused_on_the_descope_backed_routes(db_session, ory_token_source):
    """Its sole membership names a canonical tenant, which is nothing Descope can be asked about."""
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, tenant_id = await _provisioned_ory_user(db_session, client, ory_token_source, "tenant-descope")
        selected = {**headers, TENANT_SELECTOR_HEADER: tenant_id}
        documents_before = await _document_count(db_session)

        resolved = await client.get(TENANT_ROUTE, headers=selected)
        created = await client.post("/api/documents", json={"title": "a title", "content": "text"}, headers=selected)
        listed = await client.get("/api/documents", headers=selected)
        members = await client.get("/api/members", headers=selected)
        keys = await client.get("/api/keys", headers=selected)

    # The tenant resolves for this caller, so each refusal below is the route's own.
    assert resolved.json() == {"tenant_id": tenant_id}
    refusals = {"create": created, "list": listed, "members": members, "keys": keys}
    assert (members.status_code, members.json()) == (403, DESCOPE_PRINCIPALS_ONLY)
    assert {
        name: (refusals[name].status_code, refusals[name].json()["type"]) for name in ("create", "list", "keys")
    } == (dict.fromkeys(("create", "list", "keys"), CAPABILITY_MISSING))
    assert app.state.descope_client.mock_calls == []
    assert await _document_count(db_session) == documents_before


async def _assignment_count(db_session) -> int:
    return (await db_session.execute(select(func.count()).select_from(UserTenantRole))).scalar_one()


@pytest.mark.asyncio
async def test_ory_admin_cannot_assign_or_remove_a_role(db_session, ory_token_source):
    """Role assignment is mirrored to Descope, so an admin from another provider is refused before any write."""
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _provisioned_ory_user(db_session, client, ory_token_source, "tenant-roles")
        admin_tenant = await _add_membership(db_session, user_email=await _email_of(client, headers), role_name="admin")
        selected = {**headers, TENANT_SELECTOR_HEADER: admin_tenant}
        body = {"user_id": str(uuid.uuid4()), "tenant_id": admin_tenant, "role_names": ["admin"]}
        assignments_before = await _assignment_count(db_session)

        is_admin = await client.get(ADMIN_ROUTE, headers=selected)
        assigned = await client.post("/api/roles/assign", json=body, headers=selected)
        removed = await client.post("/api/roles/remove", json=body, headers=selected)

    # The caller is an admin of the tenant the body names, so each refusal is the route's own.
    assert is_admin.json() == {"tenant_id": admin_tenant, "roles": ["admin"]}
    refusals = {"assign": assigned, "remove": removed}
    assert {name: response.status_code for name, response in refusals.items()} == {"assign": 403, "remove": 403}
    assert {name: response.json() for name, response in refusals.items()} == {
        "assign": DESCOPE_PRINCIPALS_ONLY,
        "remove": DESCOPE_PRINCIPALS_ONLY,
    }
    assert app.state.descope_client.mock_calls == []
    assert await _assignment_count(db_session) == assignments_before


async def _role_count(db_session) -> int:
    return (await db_session.execute(select(func.count()).select_from(Role))).scalar_one()


async def _permission_count(db_session) -> int:
    return (await db_session.execute(select(func.count()).select_from(Permission))).scalar_one()


@pytest.mark.asyncio
async def test_ory_admin_of_one_of_two_tenants_cannot_reach_project_wide_state(db_session, ory_token_source):
    """Definitions and the FGA schema belong to no tenant: an admin of one tenant must not change them for all."""
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _provisioned_ory_user(db_session, client, ory_token_source, "tenant-project")
        admin_tenant = await _add_membership(db_session, user_email=await _email_of(client, headers), role_name="admin")
        selected = {**headers, TENANT_SELECTOR_HEADER: admin_tenant}
        roles_before = await _role_count(db_session)
        permissions_before = await _permission_count(db_session)

        is_admin = await client.get(ADMIN_ROUTE, headers=selected)
        refusals = {
            "list roles": await client.get("/api/roles", headers=selected),
            "create role": await client.post("/api/roles", json={"name": "project-wide"}, headers=selected),
            "update role": await client.put("/api/roles/admin", json={"description": "changed"}, headers=selected),
            "delete role": await client.delete("/api/roles/admin", headers=selected),
            "list permissions": await client.get("/api/permissions", headers=selected),
            "create permission": await client.post("/api/permissions", json={"name": "all.write"}, headers=selected),
            "update permission": await client.put(
                "/api/permissions/all.write", json={"new_name": "all.read"}, headers=selected
            ),
            "delete permission": await client.delete("/api/permissions/all.write", headers=selected),
            "read schema": await client.get("/api/fga/schema", headers=selected),
            "replace schema": await client.put("/api/fga/schema", json={"schema": "model AuthZ 1.0"}, headers=selected),
        }

    # The caller is an admin of the tenant it names, so each refusal is the route's own.
    assert is_admin.json() == {"tenant_id": admin_tenant, "roles": ["admin"]}
    schema = {"read schema", "replace schema"}
    assert {name: (refusals[name].status_code, refusals[name].json()["type"]) for name in schema} == dict.fromkeys(
        schema, CAPABILITY_MISSING
    )
    assert {
        name: (response.status_code, response.json()) for name, response in refusals.items() if name not in schema
    } == (dict.fromkeys(set(refusals) - schema, (403, DESCOPE_PRINCIPALS_ONLY)))
    assert app.state.descope_client.mock_calls == []
    assert await _role_count(db_session) == roles_before
    assert await _permission_count(db_session) == permissions_before


@pytest.mark.asyncio
async def test_ory_operator_of_one_of_two_tenants_cannot_list_every_tenants_users(db_session, ory_token_source):
    """The canonical user list belongs to no tenant: the header must not pick the tenant that opens it."""
    app = _build_app(db_session, issuer=ory_token_source.issuer, audience=ory_token_source.audience)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers, _ = await _provisioned_ory_user(db_session, client, ory_token_source, "tenant-global")
        operator_tenant = await _add_membership(
            db_session, user_email=await _email_of(client, headers), role_name="operator"
        )
        selected = {**headers, TENANT_SELECTOR_HEADER: operator_tenant}

        is_operator = await client.get(OPERATOR_ROUTE, headers=selected)
        named = await client.get("/api/users", headers=selected)
        unnamed = await client.get("/api/users", headers=headers)

    # The caller is an operator of the tenant it names, so the refusal is the global gate's own.
    assert is_operator.json() == {"tenant_id": operator_tenant, "roles": ["operator"]}
    assert named.status_code == 403
    assert named.json() == {"detail": "No tenant context"}
    assert unnamed.status_code == 403
    assert unnamed.json() == {"detail": "No tenant context"}
