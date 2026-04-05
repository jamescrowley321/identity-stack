import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.dependencies.identity import get_identity_service
from app.dependencies.rbac import require_role
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.identity import IdentityService

router = APIRouter(tags=["Permissions"])


def _parse_uuid(value: str, name: str) -> uuid.UUID:
    """Parse a string to UUID at the router boundary."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {name} format")


class CreatePermissionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class UpdatePermissionRequest(BaseModel):
    new_name: str = Field(min_length=1, max_length=200)
    description: str = ""


@router.get("/permissions")
async def list_permissions(
    request: Request,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """List all permission definitions. Requires owner or admin role."""
    result = await service.list_permissions()
    if result.is_error():
        return result_to_response(result, request)
    return {"permissions": result.ok}


@router.post("/permissions", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_permission(
    request: Request,
    body: CreatePermissionRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Create a new permission definition. Requires owner or admin role."""
    result = await service.create_permission(name=body.name, description=body.description)
    return result_to_response(result, request, status=201)


@router.put("/permissions/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_permission(
    request: Request,
    name: str,
    body: UpdatePermissionRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Update a permission definition. Requires owner or admin role."""
    perm_result = await service.get_permission_by_name(name=name)
    if perm_result.is_error():
        return result_to_response(perm_result, request)
    perm_id = _parse_uuid(perm_result.ok["id"], "permission_id")

    update_result = await service.update_permission(
        permission_id=perm_id,
        name=body.new_name,
        description=body.description,
    )
    return result_to_response(update_result, request)


@router.delete("/permissions/{name}")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_permission(
    request: Request,
    name: str,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    service: IdentityService = Depends(get_identity_service),
):
    """Delete a permission definition. Requires owner or admin role."""
    perm_result = await service.get_permission_by_name(name=name)
    if perm_result.is_error():
        return result_to_response(perm_result, request)
    perm_id = _parse_uuid(perm_result.ok["id"], "permission_id")

    delete_result = await service.delete_permission(permission_id=perm_id)
    if delete_result.is_error():
        return result_to_response(delete_result, request)
    return {"status": "deleted", "name": name}
