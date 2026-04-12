import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dependencies.auth import get_claims
from app.dependencies.identity import get_tenant_service
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.errors.problem_detail import result_to_response
from app.models.database import get_async_session
from app.models.tenant import TenantResource
from app.services.tenant import TenantService

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
async def create_tenant(
    request: Request,
    body: CreateTenantRequest,
    claims: dict = Depends(require_admin_role),
    tenant_service: TenantService = Depends(get_tenant_service),
):
    """Create a new tenant via the canonical identity service. Requires admin/owner role."""
    result = await tenant_service.create_tenant(
        name=body.name,
        domains=body.self_provisioning_domains,
    )
    return result_to_response(result, request)


@router.get("/tenants")
async def list_user_tenants(
    request: Request,
    tenant_claims: dict = Depends(get_tenant_claims),
):
    """List all tenants the current user belongs to, with names from Descope."""
    client = getattr(request.app.state, "descope_client", None)
    # Build base list from JWT claims
    tenants = []
    for tenant_id, info in tenant_claims.items():
        entry = {
            "id": tenant_id,
            "name": tenant_id,  # fallback to ID
            "roles": info.get("roles", []) if isinstance(info, dict) else [],
            "permissions": info.get("permissions", []) if isinstance(info, dict) else [],
        }
        # Try to load the tenant name from Descope
        if client:
            try:
                tenant_data = await client.load_tenant(tenant_id)
                if tenant_data:
                    entry["name"] = tenant_data.get("name", tenant_id)
            except Exception:  # noqa: S110
                pass  # keep the ID as fallback — non-critical for listing
        tenants.append(entry)
    return {"tenants": tenants}


@router.get("/tenants/current")
async def get_current_tenant(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
):
    """Get the current tenant context from the Descope Management API."""
    client = getattr(request.app.state, "descope_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Descope client not initialized")
    try:
        tenant_data = await client.load_tenant(tenant_id)
        if not tenant_data:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
        return {"tenant_id": tenant_id, "tenant": tenant_data}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Failed to load tenant %s from Descope: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail="Failed to load tenant from Descope") from exc


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
