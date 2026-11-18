from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.dependencies.rbac import canonical_memberships
from app.dependencies.tenant_access import SELECT_A_TENANT_FIRST, CanonicalMemberships, resolve_tenant_access
from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig

logger = logbook(__name__)

# The ``tenants`` and top-level ``roles`` claims are Descope's. Another provider's
# principal is authorized from the canonical model (or its own extractor), so
# those claims on its token are refused, not read: a realm or project mapper
# that put them there would otherwise grant tenant membership or an admin role.
DESCOPE_CLAIMS_ONLY = "This route reads Descope's tenants and roles claims, which only a Descope token carries"


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
    access = resolve_tenant_access(request, claims, memberships, no_tenant_detail=SELECT_A_TENANT_FIRST)
    return access.tenant_id


def get_descope_claims(request: Request) -> dict:
    """The request's claims, for a route that reads Descope's own ``tenants`` or ``roles`` claim.

    401 without claims. 403 for a principal whose recorded provider is not
    ``descope_managed`` (Ory, Zitadel, Keycloak), whatever its token carries.
    Claims no middleware attributed to a provider, which only a hand-built
    request has, are read as Descope's, as the tenant resolver reads them.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    provider = getattr(request.state, "provider", None)
    if isinstance(provider, ProviderTokenConfig) and not provider.descope_managed:
        logger.warning(
            "refused: provider=%s sub=%r called a route that reads Descope's tenants or roles claim",
            provider.name,
            claims.get("sub") if isinstance(claims, dict) else None,
        )
        raise HTTPException(status_code=403, detail=DESCOPE_CLAIMS_ONLY)
    return claims


def get_tenant_claims(request: Request) -> dict:
    """Return the `tenants` claim mapping from a Descope JWT (``get_descope_claims``).

    Returns an empty dict if the user has no tenant memberships. A principal of
    any other provider is refused with 403.
    """
    return get_descope_claims(request).get("tenants", {})
