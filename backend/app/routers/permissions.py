import uuid

from expression import Error, Ok
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.dependencies.identity import get_permission_service
from app.dependencies.rbac import require_role
from app.errors.identity import NotFound
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.permission import PermissionService

router = APIRouter(tags=["Permissions"])


class CreatePermissionRequest(BaseModel):
    # Descope accepts up to 255 chars; standardized across all routers
    name: str = Field(min_length=1, max_length=255)
    description: str = ""


class UpdatePermissionRequest(BaseModel):
    # Descope accepts up to 255 chars; standardized across all routers
    new_name: str = Field(min_length=1, max_length=255)
    description: str = ""


@router.get("/permissions")
async def list_permissions(
    request: Request,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    permission_service: PermissionService = Depends(get_permission_service),
):
    """List all permission definitions. Requires owner or admin role."""
    result = await permission_service.list_permissions()
    if result.is_ok():
        result = Ok({"permissions": result.ok})
    return result_to_response(result, request)


@router.post("/permissions", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_permission(
    request: Request,
    body: CreatePermissionRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    permission_service: PermissionService = Depends(get_permission_service),
):
    """Create a new permission definition. Requires owner or admin role."""
    result = await permission_service.create_permission(
        name=body.name,
        description=body.description,
    )
    return result_to_response(result, request, status=201)


@router.put("/permissions/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_permission(
    request: Request,
    name: str,
    body: UpdatePermissionRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    permission_service: PermissionService = Depends(get_permission_service),
):
    """Update a permission definition. Requires owner or admin role."""
    # Resolve permission name to canonical ID
    perms_result = await permission_service.list_permissions()
    if perms_result.is_error():
        return result_to_response(perms_result, request)
    perm_match = next((p for p in perms_result.ok if p["name"] == name), None)
    if perm_match is None:
        return result_to_response(Error(NotFound(message=f"Permission '{name}' not found")), request)

    result = await permission_service.update_permission(
        permission_id=uuid.UUID(str(perm_match["id"])),
        name=body.new_name,
        description=body.description,
    )
    return result_to_response(result, request)


@router.delete("/permissions/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_permission(
    request: Request,
    name: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    permission_service: PermissionService = Depends(get_permission_service),
):
    """Delete a permission definition. Requires owner or admin role."""
    # Resolve permission name to canonical ID
    perms_result = await permission_service.list_permissions()
    if perms_result.is_error():
        return result_to_response(perms_result, request)
    perm_match = next((p for p in perms_result.ok if p["name"] == name), None)
    if perm_match is None:
        return result_to_response(Error(NotFound(message=f"Permission '{name}' not found")), request)

    result = await permission_service.delete_permission(
        permission_id=uuid.UUID(str(perm_match["id"])),
    )
    return result_to_response(result, request)
