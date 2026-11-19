"""The guard on routes whose writes the canonical services push to the sync provider (conventions §18)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.dependencies import identity
from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig

logger = logbook(__name__)

# Its own answer, not the tenant resolver's, naming the provider the route serves.
# For Descope it is ``NOT_A_DESCOPE_PRINCIPAL`` word for word.
NOT_A_SYNC_PROVIDER_PRINCIPAL = "This route serves {provider} principals only"


def require_sync_provider_principal(request: Request) -> None:
    """Refuse a principal of any provider but the one the canonical services sync to (``SYNC_PROVIDER``).

    For the role and permission routes: ``RoleService`` and ``PermissionService``
    push every write to the configured sync adapter, which takes the request's
    tenant and user ids as that provider's. A principal of another provider is
    refused (403) before the tenant is resolved and before anything is written.
    In a Descope deployment this passes exactly the principals
    ``require_descope_principal`` passes, with the same body. Claims no
    middleware attributed to a provider are refused; a request with no claims
    gets 401 (§17).
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sync_provider = identity.SYNC_PROVIDER_PRINCIPALS.get(identity.SYNC_PROVIDER)
    provider = getattr(request.state, "provider", None)
    if sync_provider is not None and isinstance(provider, ProviderTokenConfig) and sync_provider.is_principal(provider):
        return
    logger.warning(
        "refused: provider=%s sub=%r called a route that serves principals of the sync provider %s only",
        provider.name if isinstance(provider, ProviderTokenConfig) else "unrecorded",
        claims.get("sub") if isinstance(claims, dict) else None,
        identity.SYNC_PROVIDER,
    )
    name = sync_provider.name if sync_provider is not None else identity.SYNC_PROVIDER
    raise HTTPException(status_code=403, detail=NOT_A_SYNC_PROVIDER_PRINCIPAL.format(provider=name))
