"""Dependencies that hand routers the Descope management adapter (docs/conventions.md §5)."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.descope_management import DescopeManagementAdapter


def get_descope_management(request: Request) -> DescopeManagementAdapter:
    """Build the adapter over the process-wide ``DescopeManagementClient``."""
    return DescopeManagementAdapter(request.app.state.descope_client)


def require_descope_management(request: Request) -> DescopeManagementAdapter:
    """As ``get_descope_management``, but answer 503 until the lifespan has built the client."""
    if getattr(request.app.state, "descope_client", None) is None:
        raise HTTPException(status_code=503, detail="Descope client not initialized")
    return get_descope_management(request)
