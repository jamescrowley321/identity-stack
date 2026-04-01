import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dependencies.auth import get_claims
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.models.database import get_async_session
from app.models.tenant import TenantResource
from app.services.descope import get_descope_client

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


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    self_provisioning_domains: list[str] | None = None


class CreateResourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = ""


@router.post("/tenants")
async def create_tenant(body: CreateTenantRequest, claims: dict = Depends(require_admin_role)):
    """Create a new tenant via the Descope Management API. Requires admin/owner role."""
    client = get_descope_client()
    result = await client.create_tenant(
        name=body.name,
        self_provisioning_domains=body.self_provisioning_domains,
    )
    return result


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
    tenant_id: str = Depends(get_tenant_id),
):
    """Get the current tenant context from the JWT `dct` claim."""
    try:
        client = get_descope_client()
        tenant_info = await client.load_tenant(tenant_id)
        return {"tenant_id": tenant_id, "tenant": tenant_info}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"tenant_id": tenant_id, "tenant": None}
        logger.error("Descope API error loading tenant %s: %s", tenant_id, e)
        raise HTTPException(status_code=502, detail="Failed to load tenant from Descope")
    except httpx.RequestError as e:
        logger.error("Network error loading tenant %s: %s", tenant_id, e)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API")


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
        await session.refresh(resource)
    except Exception as exc:
        logger.error("DB commit failed for tenant resource: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to create resource") from exc
    return resource.model_dump()
