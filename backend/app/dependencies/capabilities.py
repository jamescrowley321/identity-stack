"""Route dependencies that serve a route only where the deployment has its capability.

A route for a capability the configured sync adapter does not declare, or for the
relationship store where the deployment has none, answers 404 (``CapabilityMissing``)
before any authorization dependency runs: the route is not there, so no principal
gets a provider's 403 for it. Each dependency goes first in its route's list.
"""

from __future__ import annotations

from collections.abc import Callable

from app.dependencies import identity
from app.errors.identity import CapabilityMissing, IdentityErrorRaised
from app.services.adapters.base import Capability
from app.services.relationship_store import RelationshipStoreKind


def _require(capability: Capability) -> Callable[[], None]:
    def require() -> None:
        if capability not in identity.sync_capabilities():
            raise IdentityErrorRaised(CapabilityMissing(message=f"This deployment has no {capability} capability"))

    require.__name__ = f"require_{capability}"
    return require


require_machine_credentials = _require(Capability.MACHINE_CREDENTIALS)
require_custom_attributes = _require(Capability.CUSTOM_ATTRIBUTES)


def require_relationship_store() -> None:
    if identity.relationship_store_kind() is RelationshipStoreKind.NONE:
        raise IdentityErrorRaised(CapabilityMissing(message="This deployment has no relationship store"))
