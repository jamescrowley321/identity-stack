"""Dependencies that hand routers the Descope management adapter (docs/conventions.md §5)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig
from app.services.descope_management import DescopeManagementAdapter

logger = logbook(__name__)

# The answer a principal of another provider got from these routes before
# ``get_tenant_id`` could name a canonical tenant, and still gets.
NOT_A_DESCOPE_PRINCIPAL = "No tenant context — select a tenant first"


def require_descope_principal(request: Request) -> None:
    """Refuse a principal whose tenant and subject are not Descope's own (conventions §18).

    For the routes that hand the request's tenant id or the caller's ``sub`` to
    Descope's Management API or its FGA store. The tenant resolver gives a
    principal of a canonical provider a canonical tenant id, which names nothing
    in Descope, so such a principal is refused here, before the tenant is
    resolved and before Descope is called. Which providers pass is each
    provider's own ``descope_managed``.

    A request that no middleware attributed to a provider, which only a test
    that sets ``request.state`` by hand can make, passes, as it does in the
    tenant resolver.
    """
    provider = getattr(request.state, "provider", None)
    if isinstance(provider, ProviderTokenConfig) and not provider.descope_managed:
        claims = getattr(request.state, "claims", None)
        logger.warning(
            "refused: provider=%s sub=%r called a route that serves Descope principals only",
            provider.name,
            claims.get("sub") if isinstance(claims, dict) else None,
        )
        raise HTTPException(status_code=403, detail=NOT_A_DESCOPE_PRINCIPAL)


def get_descope_management(request: Request) -> DescopeManagementAdapter:
    """Build the adapter over the process-wide ``DescopeManagementClient``."""
    return DescopeManagementAdapter(request.app.state.descope_client)


def require_descope_management(request: Request) -> DescopeManagementAdapter:
    """As ``get_descope_management``, but answer 503 until the lifespan has built the client."""
    if getattr(request.app.state, "descope_client", None) is None:
        raise HTTPException(status_code=503, detail="Descope client not initialized")
    return get_descope_management(request)
