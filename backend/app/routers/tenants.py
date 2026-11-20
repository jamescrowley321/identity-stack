import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dependencies.descope import NOT_A_DESCOPE_PRINCIPAL, is_descope_principal
from app.dependencies.identity import get_tenant_service
from app.dependencies.rbac import canonical_memberships
from app.dependencies.tenant import get_descope_claims, get_tenant_id, get_tenant_memberships
from app.dependencies.tenant_access import CanonicalMemberships, TenantMembership
from app.errors.identity import NotFound
from app.errors.problem_detail import result_to_response
from app.logbook import logbook
from app.models.database import get_async_session
from app.models.tenant import TenantResource
from app.services.canonical import canonical_tenant
from app.services.tenant import TenantService

logger = logbook(__name__)

router = APIRouter(tags=["Tenants"])

# The answer to a request for a tenant the caller holds no membership in.
NOT_A_MEMBER = "Not a member of this tenant"


class CurrentTenant(BaseModel):
    """The request's tenant, in the same shape whichever provider's principal asks."""

    id: str
    name: str
    domains: list[str]
    created_at: str | None
    updated_at: str | None


class CurrentTenantResponse(BaseModel):
    tenant_id: str
    tenant: CurrentTenant


def require_admin_role(claims: dict = Depends(get_descope_claims)) -> dict:
    """Require the caller to have an 'admin' or 'owner' project-level role in its Descope ``roles`` claim.

    A principal of a provider that is not Descope-managed is refused (403) before the claim is read.
    """
    roles: list[str] = claims.get("roles", [])
    if not any(r in roles for r in ("admin", "owner")):
        raise HTTPException(status_code=403, detail="Admin or owner role required")
    return claims


def _verify_tenant_membership(tenant_id: str, memberships: dict[str, TenantMembership]) -> None:
    """Verify the caller is a member of the given tenant (memberships from ``get_tenant_memberships``)."""
    if tenant_id not in memberships:
        raise HTTPException(status_code=403, detail=NOT_A_MEMBER)


class CreateTenantRequest(BaseModel):
    # Descope accepts up to 255 chars; standardized across all routers
    name: str = Field(min_length=1, max_length=255)
    self_provisioning_domains: list[str] | None = None


class CreateResourceRequest(BaseModel):
    # Descope accepts up to 255 chars; standardized across all routers
    name: str = Field(min_length=1, max_length=255)
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
    memberships: dict[str, TenantMembership] = Depends(get_tenant_memberships),
):
    """List every tenant the caller belongs to, from the tenant resolver's sources (conventions §18).

    A canonical principal's tenants carry their canonical names. A Descope
    principal's are named from the Descope Management API, falling back to the id.
    """
    client = getattr(request.app.state, "descope_client", None) if is_descope_principal(request) else None
    tenants = []
    for membership in memberships.values():
        entry = {
            "id": membership.tenant_id,
            "name": membership.name,
            "roles": list(membership.roles),
            "permissions": list(membership.permissions),
        }
        if client:
            try:
                tenant_data = await client.load_tenant(membership.tenant_id)
                if tenant_data:
                    entry["name"] = tenant_data.get("name", membership.tenant_id)
            except Exception as exc:
                # Keep the id as the name: a name is not worth failing the list over.
                logger.debug("Failed to load the name of tenant %s from Descope: %s", membership.tenant_id, exc)
        tenants.append(entry)
    return {"tenants": tenants}


@router.get("/tenants/current", response_model=CurrentTenantResponse)
async def get_current_tenant(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    memberships: CanonicalMemberships | None = Depends(canonical_memberships),
    tenant_service: TenantService = Depends(get_tenant_service),
) -> CurrentTenantResponse:
    """Get the request's tenant (conventions §18).

    A canonical principal's tenant is read from the canonical model; a Descope
    principal's from the Descope Management API, by its Descope tenant id.
    """
    if memberships is not None:
        return await _current_canonical_tenant(request, tenant_id, tenant_service)
    if not is_descope_principal(request):
        raise HTTPException(status_code=403, detail=NOT_A_DESCOPE_PRINCIPAL)
    client = getattr(request.app.state, "descope_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Descope client not initialized")
    try:
        tenant_data = await client.load_tenant(tenant_id)
        if not tenant_data:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
        return CurrentTenantResponse(tenant_id=tenant_id, tenant=_descope_current_tenant(tenant_id, tenant_data))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Failed to load tenant %s from Descope: %s", tenant_id, exc)
        raise HTTPException(status_code=502, detail="Failed to load tenant from Descope") from exc


def _descope_current_tenant(tenant_id: str, tenant_data: dict) -> CurrentTenant:
    """A Descope tenant payload as a ``CurrentTenant``."""
    named = canonical_tenant(tenant_data)
    if not isinstance(named, dict):
        named = {}
    domains = tenant_data.get("selfProvisioningDomains")
    return CurrentTenant(
        id=tenant_id,
        name=str(tenant_data.get("name") or tenant_id),
        domains=[domain for domain in domains if isinstance(domain, str)] if isinstance(domains, list) else [],
        created_at=named.get("created_at"),
        updated_at=named.get("updated_at"),
    )


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


async def _current_canonical_tenant(request: Request, tenant_id: str, tenant_service: TenantService):
    """The canonical tenant the resolver chose, as a ``CurrentTenantResponse``."""
    try:
        canonical_id = uuid.UUID(tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found") from exc
    result = await tenant_service.get_tenant(tenant_id=canonical_id)
    if result.is_error():
        if isinstance(result.error, NotFound):
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
        return result_to_response(result, request)
    tenant = result.ok
    return CurrentTenantResponse(
        tenant_id=tenant_id,
        tenant=CurrentTenant(
            id=str(tenant["id"]),
            name=tenant["name"],
            domains=list(tenant["domains"]),
            created_at=_iso(tenant.get("created_at")),
            updated_at=_iso(tenant.get("updated_at")),
        ),
    )


@router.get("/tenants/{tenant_id}/resources")
async def list_tenant_resources(
    tenant_id: str,
    memberships: dict[str, TenantMembership] = Depends(get_tenant_memberships),
    session: AsyncSession = Depends(get_async_session),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """List resources scoped to a tenant. Only accessible if user is a member."""
    _verify_tenant_membership(tenant_id, memberships)
    statement = select(TenantResource).where(TenantResource.tenant_id == tenant_id).offset(offset).limit(limit)
    result = await session.execute(statement)
    resources = result.scalars().all()
    return {"resources": [r.model_dump() for r in resources]}


@router.post("/tenants/{tenant_id}/resources")
async def create_tenant_resource(
    tenant_id: str,
    body: CreateResourceRequest,
    memberships: dict[str, TenantMembership] = Depends(get_tenant_memberships),
    session: AsyncSession = Depends(get_async_session),
):
    """Create a resource scoped to a tenant. Only accessible if user is a member."""
    _verify_tenant_membership(tenant_id, memberships)
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
