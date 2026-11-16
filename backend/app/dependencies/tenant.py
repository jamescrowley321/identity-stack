from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.dependencies.rbac import canonical_memberships, confirmed_tenant
from app.dependencies.tenant_access import SELECT_A_TENANT_FIRST, CanonicalMemberships, resolve_tenant_access
from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig

logger = logbook(__name__)

# A provider whose tokens carry their tenant some other way has no Descope
# ``tenants`` claim to list. Refusing, not answering an empty list, keeps such a
# caller from being told it belongs to no tenant.
TENANTS_CLAIM_NOT_READ = "This route reads Descope tenant memberships, which this provider's tokens do not carry"


def get_tenant_id(
    request: Request,
    memberships: Annotated[CanonicalMemberships | None, Depends(canonical_memberships)] = None,
    confirmed: Annotated[str | None, Depends(confirmed_tenant)] = None,
) -> str:
    """The tenant this request is for, from the tenant resolver (conventions §18).

    A Descope principal's tenant is its ``dct`` claim. A principal from a provider
    in ``CANONICAL_RBAC_PROVIDERS`` gets the tenant its ``X-Tenant-ID`` header
    selects among its canonical memberships, or its sole membership. A Keycloak
    principal's tenant is the organization alias its token names: an opaque
    string, not a canonical tenant id. ``confirmed`` is the tenant the
    provider's membership lookup confirmed for a token that needs it.

    Raises 401 without claims, and 403 if the request has no single tenant.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    access = resolve_tenant_access(
        request,
        claims,
        memberships,
        no_tenant_detail=SELECT_A_TENANT_FIRST,
        confirmed_tenant=confirmed,
    )
    return access.tenant_id


def get_tenant_claims(request: Request) -> dict:
    """Return the `tenants` claim mapping from the JWT.

    Returns an empty dict if the user has no tenant memberships. ``tenants`` is
    Descope's claim: a principal of a provider that reads its tenant with its
    own extractor and is not Descope-managed (Keycloak) is refused with 403,
    whatever the token carries, before any route lists or checks it. Every
    other provider's token is read as before (#597).
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    provider = getattr(request.state, "provider", None)
    own_extractor = isinstance(provider, ProviderTokenConfig) and provider.tenant_claims is not None
    if own_extractor and not provider.descope_managed:
        logger.warning(
            "refused: provider=%s sub=%r called a route that reads the Descope tenants claim",
            provider.name,
            claims.get("sub") if isinstance(claims, dict) else None,
        )
        raise HTTPException(status_code=403, detail=TENANTS_CLAIM_NOT_READ)
    return claims.get("tenants", {})
