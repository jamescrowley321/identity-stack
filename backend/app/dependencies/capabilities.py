"""What the deployment serves the caller beyond the canonical routes, and the route gates on it.

``served`` is the one decision: ``GET /api/capabilities`` reports it, and each gate
here refuses a route whose capability it lacks. A route for a capability the caller
is not served answers 404 (``CapabilityMissing``) before any authorization
dependency runs: the route is not there, so no principal gets a provider's 403 for
it. Each gate goes first in its route's list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import identity
from app.dependencies.descope import is_descope_principal
from app.errors.identity import CapabilityMissing, IdentityErrorRaised
from app.logbook import logbook
from app.models.database import get_async_session
from app.services.adapters.base import Capability, CustomAttributes
from app.services.relationship_store import RelationshipStoreKind

logger = logbook(__name__)


@dataclass(frozen=True)
class Served:
    capabilities: frozenset[Capability]
    relationship_store: RelationshipStoreKind


NOTHING = Served(frozenset(), RelationshipStoreKind.NONE)


def served(request: Request) -> Served:
    """The sync adapter's capabilities and the relationship store, as far as they serve the caller.

    An adapter's capabilities take the caller's ``sub`` and tenant as its own
    provider's ids, so they serve only that provider's principals
    (``identity.is_sync_provider_principal``; for Descope, a ``descope_managed``
    provider's, conventions §18): any other principal is served none. Descope's FGA
    store is keyed on Descope's ids too, so it serves only a Descope principal.
    """
    if getattr(request.state, "claims", None) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    descope_principal = is_descope_principal(request)
    capabilities = identity.sync_capabilities() if identity.is_sync_provider_principal(request) else frozenset()
    store = identity.relationship_store_kind()
    if store is RelationshipStoreKind.DESCOPE and not descope_principal:
        store = RelationshipStoreKind.NONE
    return Served(capabilities, store)


def _require(capability: Capability) -> Callable[[Request], None]:
    def require(request: Request) -> None:
        if capability not in served(request).capabilities:
            raise _missing(capability)

    require.__name__ = f"require_{capability}"
    return require


require_machine_credentials = _require(Capability.MACHINE_CREDENTIALS)
require_custom_attributes = _require(Capability.CUSTOM_ATTRIBUTES)


def require_relationship_store(request: Request) -> None:
    if served(request).relationship_store is RelationshipStoreKind.NONE:
        raise IdentityErrorRaised(CapabilityMissing(message="This deployment has no relationship store"))


def _missing(capability: Capability) -> IdentityErrorRaised:
    return IdentityErrorRaised(CapabilityMissing(message=f"This deployment has no {capability} capability"))


async def custom_attributes_if_served(
    request: Request, session: AsyncSession = Depends(get_async_session)
) -> CustomAttributes | None:
    """The sync adapter, as its ``custom_attributes`` capability, when it serves the caller (``served``)."""
    if Capability.CUSTOM_ATTRIBUTES not in served(request).capabilities:
        return None
    adapter = identity.get_sync_adapter(request, session)
    return adapter if isinstance(adapter, CustomAttributes) else None


async def custom_attributes_if_available(
    request: Request, session: AsyncSession = Depends(get_async_session)
) -> CustomAttributes | None:
    """As ``custom_attributes_if_served``, for an informational read: an adapter that cannot be built is ``None``.

    A refusal (401, a capability error) still answers; any other failure to build the
    adapter is logged and leaves the read without custom attributes.
    """
    try:
        return await custom_attributes_if_served(request, session)
    except (HTTPException, IdentityErrorRaised):
        raise
    except Exception:
        logger.exception("the sync adapter's custom attributes could not be built")
        return None


async def get_custom_attributes(
    adapter: CustomAttributes | None = Depends(custom_attributes_if_served),
) -> CustomAttributes:
    """As ``custom_attributes_if_served``, for a route gated by ``require_custom_attributes``: 404 otherwise."""
    if adapter is None:
        raise _missing(Capability.CUSTOM_ATTRIBUTES)
    return adapter


def sync_provider_subject(request: Request) -> str | None:
    """The caller's ``sub`` when they are a principal of the sync provider, else ``None``.

    Only then is the ``sub`` an account of the sync provider, so only then may a change
    of the caller's own profile be pushed to it (``OWN_PROFILE_OF``). 401 without claims.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sub = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(sub, str) or not sub or not identity.is_sync_provider_principal(request):
        return None
    return sub
