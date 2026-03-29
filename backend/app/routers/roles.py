import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.descope import get_descope_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Roles"])


class RoleAssignmentRequest(BaseModel):
    user_id: str
    tenant_id: str
    role_names: list[str] = Field(min_length=1)


class CreateRoleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    permission_names: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list, max_length=100)


class UpdateRoleRequest(BaseModel):
    new_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    permission_names: list[Annotated[str, Field(min_length=1)]] | None = Field(default=None, max_length=100)


# --- Existing user-facing endpoints (must stay before /roles/{name}) ---


@router.get("/roles/me")
async def get_my_roles(
    tenant_id: str = Depends(get_tenant_id),
    tenant_claims: dict = Depends(get_tenant_claims),
):
    """Get the current user's roles and permissions in their active tenant."""
    tenant_info = tenant_claims.get(tenant_id, {})
    if not isinstance(tenant_info, dict):
        tenant_info = {}
    return {
        "tenant_id": tenant_id,
        "roles": tenant_info.get("roles", []),
        "permissions": tenant_info.get("permissions", []),
    }


@router.post("/roles/assign")
@limiter.limit(RATE_LIMIT_AUTH)
async def assign_roles(
    request: Request,
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Assign roles to a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    if "owner" in body.role_names and "owner" not in admin_roles:
        raise HTTPException(status_code=403, detail="Only owners can assign the owner role")
    try:
        client = get_descope_client()
        await client.assign_roles(body.user_id, body.tenant_id, body.role_names)
        return {"status": "roles_assigned", "user_id": body.user_id, "role_names": body.role_names}
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        logger.warning("Descope API error assigning roles: %s %s", exc.response.status_code, body)
        raise HTTPException(status_code=502, detail=f"Descope API {exc.response.status_code}: {body}") from exc
    except httpx.RequestError as exc:
        logger.error("Network error assigning roles: %s", exc)
        raise HTTPException(status_code=502, detail=f"Network error: {exc}") from exc


@router.post("/roles/remove")
@limiter.limit(RATE_LIMIT_AUTH)
async def remove_roles(
    request: Request,
    body: RoleAssignmentRequest,
    current_tenant: str = Depends(get_tenant_id),
    admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Remove roles from a user in a tenant. Requires owner or admin role."""
    if body.tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot manage roles for a different tenant")
    if "owner" in body.role_names and "owner" not in admin_roles:
        raise HTTPException(status_code=403, detail="Only owners can remove the owner role")
    try:
        client = get_descope_client()
        await client.remove_roles(body.user_id, body.tenant_id, body.role_names)
        return {"status": "roles_removed", "user_id": body.user_id, "role_names": body.role_names}
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        logger.warning("Descope API error removing roles: %s %s", exc.response.status_code, body)
        raise HTTPException(status_code=502, detail=f"Descope API {exc.response.status_code}: {body}") from exc
    except httpx.RequestError as exc:
        logger.error("Network error removing roles: %s", exc)
        raise HTTPException(status_code=502, detail=f"Network error: {exc}") from exc


# --- Role definition CRUD (admin-only) ---


@router.get("/roles")
async def list_roles(
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List all role definitions. Requires owner or admin role."""
    try:
        client = get_descope_client()
        roles = await client.list_roles()
        return {"roles": roles}
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        logger.warning("Descope API error listing roles: %s %s", exc.response.status_code, body)
        raise HTTPException(status_code=502, detail=f"Descope API {exc.response.status_code}: {body}") from exc
    except httpx.RequestError as exc:
        logger.error("Network error listing roles: %s", exc)
        raise HTTPException(status_code=502, detail=f"Network error: {exc}") from exc


@router.post("/roles", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_role(
    request: Request,
    body: CreateRoleRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Create a new role definition. Requires owner or admin role."""
    try:
        client = get_descope_client()
        await client.create_role(body.name, body.description, body.permission_names or None)
        return {"name": body.name, "description": body.description, "permission_names": body.permission_names}
    except httpx.HTTPStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error creating role '%s': %s %s", body.name, exc.response.status_code, resp_body)
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=f"Invalid role data: {resp_body}") from exc
        if exc.response.status_code == 409:
            raise HTTPException(status_code=409, detail="Role already exists") from exc
        raise HTTPException(status_code=502, detail=f"Descope API {exc.response.status_code}: {resp_body}") from exc
    except httpx.RequestError as exc:
        logger.error("Network error creating role '%s': %s", body.name, exc)
        raise HTTPException(status_code=502, detail=f"Network error: {exc}") from exc


@router.put("/roles/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_role(
    request: Request,
    name: str,
    body: UpdateRoleRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Update a role definition. Requires owner or admin role."""
    try:
        client = get_descope_client()
        effective_name = body.new_name if body.new_name is not None else name
        await client.update_role(name, effective_name, body.description, body.permission_names)
        return {"name": effective_name, "description": body.description, "permission_names": body.permission_names}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error updating role '%s': %s", name, exc.response.status_code)
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Role not found") from exc
        if exc.response.status_code == 409:
            raise HTTPException(status_code=409, detail="Role name conflict") from exc
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail="Invalid role data") from exc
        raise HTTPException(status_code=502, detail="Failed to update role in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error updating role '%s': %s", name, exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.delete("/roles/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_role(
    request: Request,
    name: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Delete a role definition. Requires owner or admin role."""
    try:
        client = get_descope_client()
        await client.delete_role(name)
        return {"status": "deleted", "name": name}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error deleting role '%s': %s", name, exc.response.status_code)
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Role not found") from exc
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail="Invalid role data") from exc
        raise HTTPException(status_code=502, detail="Failed to delete role in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error deleting role '%s': %s", name, exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc
