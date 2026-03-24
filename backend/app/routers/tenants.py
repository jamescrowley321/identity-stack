from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies.auth import get_claims
from app.dependencies.tenant import get_tenant_claims, get_tenant_id
from app.models.database import get_session
from app.models.tenant import TenantResource
from app.services.descope import get_descope_client

router = APIRouter()


class CreateTenantRequest(BaseModel):
    name: str
    self_provisioning_domains: list[str] | None = None


class CreateResourceRequest(BaseModel):
    name: str
    description: str = ""


@router.post("/tenants")
async def create_tenant(body: CreateTenantRequest, claims: dict = Depends(get_claims)):
    """Create a new tenant via the Descope Management API."""
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
    except Exception:
        return {"tenant_id": tenant_id, "tenant": None}


@router.get("/tenants/{tenant_id}/resources")
async def list_tenant_resources(
    tenant_id: str,
    current_tenant: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """List resources scoped to a tenant. Only accessible if user is in that tenant."""
    if tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot access resources for a different tenant")
    resources = session.exec(select(TenantResource).where(TenantResource.tenant_id == tenant_id)).all()
    return {"resources": [r.model_dump() for r in resources]}


@router.post("/tenants/{tenant_id}/resources")
async def create_tenant_resource(
    tenant_id: str,
    body: CreateResourceRequest,
    current_tenant: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """Create a resource scoped to a tenant. Only accessible if user is in that tenant."""
    if tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Cannot create resources for a different tenant")
    resource = TenantResource(tenant_id=tenant_id, name=body.name, description=body.description)
    session.add(resource)
    session.commit()
    session.refresh(resource)
    return resource.model_dump()
