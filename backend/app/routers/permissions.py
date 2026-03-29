import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.dependencies.rbac import require_role
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.descope import get_descope_client

logger = logging.getLogger(__name__)

router = APIRouter()


class CreatePermissionRequest(BaseModel):
    name: str
    description: str = ""


class UpdatePermissionRequest(BaseModel):
    new_name: str
    description: str = ""


@router.get("/permissions")
async def list_permissions(
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List all permission definitions. Requires owner or admin role."""
    try:
        client = get_descope_client()
        permissions = await client.list_permissions()
        return {"permissions": permissions}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error listing permissions: %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to list permissions from Descope")
    except httpx.RequestError as exc:
        logger.error("Network error listing permissions: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API")


@router.post("/permissions", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_permission(
    request: Request,
    body: CreatePermissionRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Create a new permission definition. Requires owner or admin role."""
    try:
        client = get_descope_client()
        await client.create_permission(body.name, body.description)
        return {"name": body.name, "description": body.description}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error creating permission '%s': %s", body.name, exc.response.status_code)
        if exc.response.status_code < 500:
            detail = f"Failed to create permission: {exc.response.text}"
            raise HTTPException(status_code=exc.response.status_code, detail=detail)
        raise HTTPException(status_code=502, detail="Failed to create permission in Descope")
    except httpx.RequestError as exc:
        logger.error("Network error creating permission '%s': %s", body.name, exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API")


@router.put("/permissions/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_permission(
    request: Request,
    name: str,
    body: UpdatePermissionRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Update a permission definition. Requires owner or admin role."""
    try:
        client = get_descope_client()
        await client.update_permission(name, body.new_name, body.description)
        return {"name": body.new_name, "description": body.description}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error updating permission '%s': %s", name, exc.response.status_code)
        if exc.response.status_code < 500:
            detail = f"Failed to update permission: {exc.response.text}"
            raise HTTPException(status_code=exc.response.status_code, detail=detail)
        raise HTTPException(status_code=502, detail="Failed to update permission in Descope")
    except httpx.RequestError as exc:
        logger.error("Network error updating permission '%s': %s", name, exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API")


@router.delete("/permissions/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_permission(
    request: Request,
    name: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Delete a permission definition. Requires owner or admin role."""
    try:
        client = get_descope_client()
        await client.delete_permission(name)
        return {"status": "deleted", "name": name}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error deleting permission '%s': %s", name, exc.response.status_code)
        if exc.response.status_code < 500:
            detail = f"Failed to delete permission: {exc.response.text}"
            raise HTTPException(status_code=exc.response.status_code, detail=detail)
        raise HTTPException(status_code=502, detail="Failed to delete permission in Descope")
    except httpx.RequestError as exc:
        logger.error("Network error deleting permission '%s': %s", name, exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API")
