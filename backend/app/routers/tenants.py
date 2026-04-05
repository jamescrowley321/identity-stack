import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dependencies.auth import get_claims
from app.dependencies.identity import get_identity_service
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.errors.problem_detail import result_to_response
from app.models.database import get_async_session
from app.models.tenant import TenantResource
from app.services.identity import IdentityService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Tenants"])


def require_admin_role(claims: dict = Depends(get_claims)) -> dict:
    """Require the caller to have an 'admin' or 'owner' project-level role."""
    roles: list[str] = claims.get("roles", [])
    if not any(r in roles for r in ("admin", "owner")):
        raise HTTPException(status_code=403, detail="Admin or owner role required")
    return claims


def _verify_tenant_membership(tenant_id: str, tenant_claims: dict) -> None:
    """Verify the user is a member of the given tenant."""
    if tenant_id not in tenant_claims:
        raise HTTPException(status_code=403, detail="Not a member of this tenant")


def _parse_uuid(value: str, name: str) -> uuid.UUID:
    """Parse a string to UUID at the router boundary."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {name} format")


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    self_provisioning_domains: list[str] | None = None


class CreateResourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = ""


@router.post("/tenants")
async def create_tenant(
    request: Request,
    body: CreateTenantRequest,
    claims: dict = Depends(require_admin_role),
    service: IdentityService = Depends(get_identity_service),
):
    """Create a new tenant via IdentityService. Requires admin/owner role."""
    result = await service.create_tenant(
        name=body.name,
        domains=body.self_provisioning_domains,
    )
    return result_to_response(result, request, status=201)


@router.get("/tenants")
async def list_user_tenants(tenant_claims: dict = Depends(get_tenant_claims)):
    """List all tenants the current user belongs to (from JWT claims)."""
    tenants = [
        {
            "id": tenant_id,
            "roles": info.get("roles", []) if isinstance(info, dict) else [],
            "permissions": info.get("permissions", []) if isinstance(info, dict) else [],
        }
        for tenant_id, info in tenant_claims.items()
    ]
    return {"tenants": tenants}


@router.get("/tenants/current")
async def get_current_tenant(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    service: IdentityService = Depends(get_identity_service),
):
    """Get the current tenant context from the JWT `dct` claim."""
    tid = _parse_uuid(tenant_id, "tenant_id")
    result = await service.get_tenant(tenant_id=tid)
    if result.is_ok():
        return {"tenant_id": tenant_id, "tenant": result.ok}
    return {"tenant_id": tenant_id, "tenant": None}


@router.get("/tenants/{tenant_id}/resources")
async def list_tenant_resources(
    tenant_id: str,
    tenant_claims: dict = Depends(get_tenant_claims),
    session: AsyncSession = Depends(get_async_session),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """List resources scoped to a tenant. Only accessible if user is a member."""
    _verify_tenant_membership(tenant_id, tenant_claims)
    statement = select(TenantResource).where(TenantResource.tenant_id == tenant_id).offset(offset).limit(limit)
    result = await session.execute(statement)
    resources = result.scalars().all()
    return {"resources": [r.model_dump() for r in resources]}


@router.post("/tenants/{tenant_id}/resources")
async def create_tenant_resource(
    tenant_id: str,
    body: CreateResourceRequest,
    tenant_claims: dict = Depends(get_tenant_claims),
    session: AsyncSession = Depends(get_async_session),
):
    """Create a resource scoped to a tenant. Only accessible if user is a member."""
    _verify_tenant_membership(tenant_id, tenant_claims)
    resource = TenantResource(tenant_id=tenant_id, name=body.name, description=body.description)
    try:
        session.add(resource)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        logger.warning("Duplicate tenant resource name '%s' in tenant %s", body.name, tenant_id)
        raise HTTPException(status_code=409, detail="A resource with that name already exists") from exc
    except Exception as exc:
        await session.rollback()
        logger.error("DB commit failed for tenant resource: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to create resource") from exc

    try:
        await session.refresh(resource)
    except Exception:
        logger.warning("DB refresh failed for tenant resource after successful commit")

    return resource.model_dump()
