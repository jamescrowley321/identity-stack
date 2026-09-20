"""Role and permission dependencies.

Two shapes of principal reach these dependencies:

* Providers that carry authorization **in the token** — Descope, via its ``dct``
  (current tenant) and ``tenants`` (memberships with roles/permissions) claims.
  These are decided from the claims alone, with no database read, exactly as
  before.
* Providers that carry **no** authorization claims — Ory, and standard OIDC
  generally. Their tokens say who the caller is and nothing about what the caller
  may do, so roles and permissions are resolved from the canonical model instead:
  the same ``(provider, sub)`` lookup that backs ``GET /api/identity``.

Epic 3 moved roles and tenants into the canonical model but left the authorization
path reading Descope claim shapes, which meant every role-gated route answered 403
"No tenant context" for an Ory principal. This closes that gap without changing
what a Descope token authorizes.

Which providers take the canonical path is configuration, not code:
``CANONICAL_RBAC_PROVIDERS`` (default ``ory``), mirroring ``JIT_ENABLED_PROVIDERS``
in ``app.routers.protected``. Descope is deliberately absent from the default set —
its authorization decisions still come only from its own signed claims.

Every branch of the canonical path that cannot produce an unambiguous answer fails
closed with 403. An authorization decision is never made on a guess.
"""

import logging
import os
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_session_factory
from app.models.identity.user import UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.identity_resolution import IdentityResolutionService

logger = logging.getLogger(__name__)

# Providers whose tokens carry no authorization claims — their roles and
# permissions come from the canonical model. Descope is excluded by default:
# it signs its own `dct`/`tenants` claims and those stay authoritative.
CANONICAL_RBAC_PROVIDERS = {
    name.strip().lower() for name in os.getenv("CANONICAL_RBAC_PROVIDERS", "ory").split(",") if name.strip()
}


async def get_rbac_session() -> AsyncIterator[AsyncSession | None]:
    """Yield a session for canonical role resolution, or None when there is no database.

    Separate from ``get_async_session`` on purpose. This dependency resolves on
    every role-gated route, including the access-key routes that talk only to the
    identity provider and have no database dependency of their own. Those must keep
    working wherever they work today, so a missing or malformed ``DATABASE_URL``
    yields None here and the canonical path fails closed with 403 rather than
    turning a 403 into a 500.

    Creating a session is lazy — SQLAlchemy acquires no connection until a
    statement runs — so a request decided from the token alone pays nothing for it.
    """
    try:
        factory = get_session_factory()
    except RuntimeError:
        logger.debug("no database configured; canonical role resolution unavailable")
        yield None
        return
    async with factory() as session:
        yield session


def _principal_provider(request: Request) -> str:
    """The name of the provider that authenticated this request, lowercased."""
    principal = getattr(request.state, "principal", None)
    identity = getattr(principal, "identity", None)
    authentication_type = getattr(identity, "authentication_type", None)
    return authentication_type.lower() if isinstance(authentication_type, str) else ""


async def _canonical_grants(
    request: Request,
    claims: dict,
    session: AsyncSession | None,
) -> tuple[list[str], list[str]]:
    """Resolve (roles, permissions) for the caller's tenant from the canonical model.

    Raises 401 when the token names no subject, and 403 on every branch that cannot
    name exactly one tenant — zero memberships, or several with nothing in the token
    to choose between them. Picking one would be an arbitrary grant.
    """
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject")
    if session is None:
        raise HTTPException(status_code=403, detail="No tenant context")

    resolver = IdentityResolutionService(
        user_repository=UserRepository(session),
        idp_link_repository=IdPLinkRepository(session),
        provider_repository=ProviderRepository(session),
        assignment_repository=UserTenantRoleRepository(session),
        role_repository=RoleRepository(session),
        tenant_repository=TenantRepository(session),
        # Uncached deliberately: an authorization decision must not be served from
        # an identity snapshot that a role change up to IDENTITY_CACHE_TTL seconds
        # ago has already made wrong. The read path may tolerate that staleness;
        # the authz path may not.
        redis_client=None,
    )
    result = await resolver.resolve(provider=_principal_provider(request), sub=sub)
    if result.is_error():
        logger.debug("canonical role resolution found no identity for sub=%r", sub)
        raise HTTPException(status_code=403, detail="No tenant context")

    # Deactivation is the only revocation signal that reaches this path. Every
    # control that offboards a user — ``UserService.deactivate_user``, the Descope
    # ``user.deleted`` webhook, and reconciliation's ``disabled``/``invited`` mapping
    # — sets ``status`` and leaves the ``user_tenant_roles`` rows intact. For a
    # provider in ``CANONICAL_RBAC_PROVIDERS`` the canonical model is the sole
    # authorization source and nothing disables the upstream account, so a non-active
    # user must be refused here or offboarding revokes nothing at all. Unknown values
    # are refused for the same reason: this fails closed.
    user_status = (result.ok.get("user") or {}).get("status")
    if user_status != UserStatus.active.value:
        logger.warning(
            "canonical role resolution refused a non-active user (status=%r)",
            user_status,
        )
        raise HTTPException(status_code=403, detail="No tenant context")

    grants = result.ok.get("roles", [])
    tenant_ids = {grant["tenant_id"] for grant in grants}

    claimed_tenant = claims.get("dct")
    if claimed_tenant is not None and not isinstance(claimed_tenant, str):
        # ``dct`` arrives straight from the token. A list or dict raises
        # ``TypeError: unhashable type`` on the membership test below and surfaces as
        # a 500 out of an auth dependency — the one branch here that would not fail
        # closed.
        raise HTTPException(status_code=403, detail="No tenant context")
    if claimed_tenant:
        # A provider in this set does not normally emit `dct`, but if a token does
        # carry one it must name a tenant the canonical model actually grants.
        if claimed_tenant not in tenant_ids:
            raise HTTPException(status_code=403, detail="No tenant context")
        tenant_id = claimed_tenant
    elif len(tenant_ids) == 1:
        tenant_id = next(iter(tenant_ids))
    else:
        raise HTTPException(status_code=403, detail="No tenant context")

    roles = [grant["role_name"] for grant in grants if grant["tenant_id"] == tenant_id]
    permissions = [
        permission for grant in grants if grant["tenant_id"] == tenant_id for permission in grant.get("permissions", [])
    ]
    return roles, permissions


def require_role(*roles: str):
    """Dependency factory that enforces the user has one of the specified roles
    in their current tenant.

    Descope principals are decided from the ``dct`` + ``tenants`` JWT claims;
    principals from a provider in ``CANONICAL_RBAC_PROVIDERS`` are decided from the
    canonical model.

    Returns the user's role list for downstream use.
    """

    async def dependency(
        request: Request,
        session: AsyncSession | None = Depends(get_rbac_session),
    ) -> list[str]:
        claims = getattr(request.state, "claims", None)
        if claims is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        if _principal_provider(request) in CANONICAL_RBAC_PROVIDERS:
            user_roles, _ = await _canonical_grants(request, claims, session)
        else:
            tenant_id = claims.get("dct")
            if not tenant_id:
                raise HTTPException(status_code=403, detail="No tenant context")
            tenant_info = claims.get("tenants", {}).get(tenant_id, {})
            user_roles = tenant_info.get("roles", []) if isinstance(tenant_info, dict) else []

        if not any(r in user_roles for r in roles):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user_roles

    return dependency


def require_permission(*permissions: str):
    """Dependency factory that enforces the user has one of the specified permissions
    in their current tenant.

    Descope principals are decided from the ``dct`` + ``tenants`` JWT claims;
    principals from a provider in ``CANONICAL_RBAC_PROVIDERS`` are decided from the
    canonical model.

    Returns the user's permission list for downstream use.
    """

    async def dependency(
        request: Request,
        session: AsyncSession | None = Depends(get_rbac_session),
    ) -> list[str]:
        claims = getattr(request.state, "claims", None)
        if claims is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        if _principal_provider(request) in CANONICAL_RBAC_PROVIDERS:
            _, user_permissions = await _canonical_grants(request, claims, session)
        else:
            tenant_id = claims.get("dct")
            if not tenant_id:
                raise HTTPException(status_code=403, detail="No tenant context")
            tenant_info = claims.get("tenants", {}).get(tenant_id, {})
            user_permissions = tenant_info.get("permissions", []) if isinstance(tenant_info, dict) else []

        if not any(p in user_permissions for p in permissions):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user_permissions

    return dependency
