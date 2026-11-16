"""Dependencies that hand routers the Descope management adapter (docs/conventions.md §5)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.dependencies.rbac import refuses_from_the_token_alone
from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig
from app.services.descope_management import DescopeManagementAdapter

logger = logbook(__name__)

# Its own answer, not the tenant resolver's: selecting a tenant would not help
# a principal of another provider, so the body does not tell them to.
NOT_A_DESCOPE_PRINCIPAL = "This route serves Descope principals only"


def is_descope_principal(request: Request) -> bool:
    """Whether a middleware attributed the request's claims to a ``descope_managed`` provider.

    False for claims no provider is recorded for: they are never read as Descope's.
    """
    provider = getattr(request.state, "provider", None)
    return isinstance(provider, ProviderTokenConfig) and provider.descope_managed


@refuses_from_the_token_alone
def require_descope_principal(request: Request) -> None:
    """Refuse a principal whose tenant and subject are not Descope's own (conventions §18).

    For the routes that hand the request's tenant id or the caller's ``sub`` to
    Descope's Management API or its FGA store. The tenant resolver gives a
    principal of a canonical provider a canonical tenant id, which names nothing
    in Descope, so such a principal is refused here, before the tenant is
    resolved and before Descope is called. Which providers pass is each
    provider's own ``descope_managed``.

    It passes only a principal it knows to be Descope's. Claims that no
    middleware attributed to a provider are refused like another provider's:
    both middlewares record the provider beside the claims, so such a request
    is a defect, and it must not be read as a Descope principal. A request with
    no claims at all is answered 401 here, as by every other authorization
    dependency (conventions §17): the guard does not rely on a second
    dependency of the route to refuse it.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if is_descope_principal(request):
        return
    provider = getattr(request.state, "provider", None)
    logger.warning(
        "refused: provider=%s sub=%r called a route that serves Descope principals only",
        provider.name if isinstance(provider, ProviderTokenConfig) else "unrecorded",
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
