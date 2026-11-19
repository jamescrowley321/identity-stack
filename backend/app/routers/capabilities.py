"""``GET /api/capabilities``: what the deployment serves beyond the canonical routes.

The SPA shows a page only when its capability is here. Any signed-in principal may
read it: it describes the deployment, not the caller or a tenant.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies.auth import get_claims
from app.dependencies.identity import relationship_store_kind, sync_capabilities
from app.services.adapters.base import Capability
from app.services.relationship_store import RelationshipStoreKind

router = APIRouter(tags=["Capabilities"])


class CapabilitiesResponse(BaseModel):
    capabilities: list[Capability]
    relationship_store: RelationshipStoreKind


@router.get("/capabilities", dependencies=[Depends(get_claims)])
async def get_capabilities() -> CapabilitiesResponse:
    """The sync adapter's capabilities, sorted, and the relationship store's kind."""
    return CapabilitiesResponse(
        capabilities=sorted(sync_capabilities()),
        relationship_store=relationship_store_kind(),
    )
