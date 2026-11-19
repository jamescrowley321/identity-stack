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

from fastapi import HTTPException, Request

from app.dependencies import identity
from app.dependencies.descope import is_descope_principal
from app.errors.identity import CapabilityMissing, IdentityErrorRaised
from app.services.adapters.base import Capability
from app.services.adapters.descope import DescopeSyncAdapter
from app.services.relationship_store import RelationshipStoreKind


@dataclass(frozen=True)
class Served:
    capabilities: frozenset[Capability]
    relationship_store: RelationshipStoreKind


NOTHING = Served(frozenset(), RelationshipStoreKind.NONE)


def served(request: Request) -> Served:
    """The sync adapter's capabilities and the relationship store, as far as they serve the caller.

    Descope's capabilities and its FGA store are keyed on Descope's own tenant and
    user ids, so they serve only a principal of a ``descope_managed`` provider
    (conventions §18): any other principal is served neither.
    """
    if getattr(request.state, "claims", None) is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    descope_principal = is_descope_principal(request)
    capabilities = identity.sync_capabilities()
    if identity.SYNC_ADAPTERS[identity.SYNC_PROVIDER].adapter is DescopeSyncAdapter and not descope_principal:
        capabilities = frozenset()
    store = identity.relationship_store_kind()
    if store is RelationshipStoreKind.DESCOPE and not descope_principal:
        store = RelationshipStoreKind.NONE
    return Served(capabilities, store)


def _require(capability: Capability) -> Callable[[Request], None]:
    def require(request: Request) -> None:
        if capability not in served(request).capabilities:
            raise IdentityErrorRaised(CapabilityMissing(message=f"This deployment has no {capability} capability"))

    require.__name__ = f"require_{capability}"
    return require


require_machine_credentials = _require(Capability.MACHINE_CREDENTIALS)
require_custom_attributes = _require(Capability.CUSTOM_ATTRIBUTES)


def require_relationship_store(request: Request) -> None:
    if served(request).relationship_store is RelationshipStoreKind.NONE:
        raise IdentityErrorRaised(CapabilityMissing(message="This deployment has no relationship store"))
