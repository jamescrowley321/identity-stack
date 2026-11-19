"""``GET /api/capabilities``: what the deployment serves beyond the canonical routes.

The SPA shows a page only when its capability is here. Any signed-in principal may
read it: it describes the deployment as it serves the caller (``served``), not a tenant.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.dependencies.capabilities import Served, served
from app.services.adapters.base import Capability
from app.services.relationship_store import RelationshipStoreKind

router = APIRouter(tags=["Capabilities"])


class CapabilitiesResponse(BaseModel):
    capabilities: list[Capability]
    relationship_store: RelationshipStoreKind


@router.get("/capabilities")
async def get_capabilities(caller: Served = Depends(served)) -> CapabilitiesResponse:
    """The capabilities, sorted, and the relationship store's kind that serve the caller."""
    return CapabilitiesResponse(capabilities=sorted(caller.capabilities), relationship_store=caller.relationship_store)
