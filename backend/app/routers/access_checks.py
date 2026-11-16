"""Two routes that answer from the caller's tenant and permissions alone.

Each depends on ``get_tenant_id`` and ``require_permission`` and on nothing
else: the handler calls no provider and reads no table. The resolver may ask
the provider's membership lookup first, for a token that needs its sole
membership confirmed (an organization-bound Keycloak token). They show what the tenant
resolver decided for the request (conventions §18), for a principal of any
provider, and give an end-to-end run a route that needs a read permission and
one that needs a management permission.
"""

from fastapi import APIRouter, Depends

from app.dependencies.rbac import require_permission
from app.dependencies.tenant import get_tenant_id

router = APIRouter(prefix="/access-checks", tags=["Access checks"])

DOCUMENTS_READ = "documents:read"
SETTINGS_MANAGE = "settings:manage"


def _granted(tenant_id: str, permission: str) -> dict[str, str]:
    return {"tenant_id": tenant_id, "permission": permission}


@router.get("/documents-read", dependencies=[Depends(require_permission(DOCUMENTS_READ))])
async def documents_read(tenant_id: str = Depends(get_tenant_id)):
    """Answer the request's tenant to a caller who holds ``documents:read`` in it."""
    return _granted(tenant_id, DOCUMENTS_READ)


@router.get("/settings-manage", dependencies=[Depends(require_permission(SETTINGS_MANAGE))])
async def settings_manage(tenant_id: str = Depends(get_tenant_id)):
    """Answer the request's tenant to a caller who holds ``settings:manage`` in it."""
    return _granted(tenant_id, SETTINGS_MANAGE)
