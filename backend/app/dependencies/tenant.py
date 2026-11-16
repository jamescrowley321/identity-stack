from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.dependencies.rbac import canonical_memberships
from app.dependencies.tenant_access import CanonicalMemberships, resolve_tenant_access


def get_tenant_id(
    request: Request,
    memberships: Annotated[CanonicalMemberships | None, Depends(canonical_memberships)] = None,
) -> str:
    """The tenant this request is for, from the tenant resolver (conventions §18).

    A Descope principal's tenant is its ``dct`` claim. A principal from a provider
    in ``CANONICAL_RBAC_PROVIDERS`` gets the tenant its ``X-Tenant-ID`` header
    selects among its canonical memberships, or its sole membership.

    Raises 401 without claims, and 403 if the request has no single tenant.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    access = resolve_tenant_access(
        request,
        claims,
        memberships,
        no_tenant_detail="No tenant context — select a tenant first",
    )
    return access.tenant_id


def get_tenant_claims(request: Request) -> dict:
    """Return the `tenants` claim mapping from the JWT.

    Returns an empty dict if the user has no tenant memberships.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return claims.get("tenants", {})
