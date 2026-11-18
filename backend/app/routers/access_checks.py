"""Routes that answer from the caller's tenant and grants alone.

Two depend on ``get_tenant_id`` and ``require_permission`` and on nothing
else: the handler calls no provider and reads no table. The resolver may ask
the provider's membership lookup first, for a token that needs its sole
membership confirmed (an organization-bound Keycloak token). They show what the tenant
resolver decided for the request (conventions §18), for a principal of any
provider, and give an end-to-end run a route that needs a read permission and
one that needs a management permission.

Two more depend on ``require_role`` and on nothing else, and answer the roles the
caller holds in the request's tenant: one needs ``owner`` or ``admin``, the other
any canonical role. They give an end-to-end run a route gated on a role, for a
provider whose authorization is canonical (Ory, Zitadel). A Keycloak token
carries no role, so a Keycloak principal is refused on both.
"""

from fastapi import APIRouter, Depends

from app.dependencies.rbac import require_permission, require_role
from app.dependencies.tenant import get_tenant_id

router = APIRouter(prefix="/access-checks", tags=["Access checks"])

DOCUMENTS_READ = "documents:read"
SETTINGS_MANAGE = "settings:manage"
ADMIN_ROLES = ("owner", "admin")
CANONICAL_ROLES = ("owner", "admin", "member", "viewer")


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


@router.get("/admin-role")
async def admin_role(roles: list[str] = Depends(require_role(*ADMIN_ROLES))):
    """Answer the roles of a caller who is ``owner`` or ``admin`` in the request's tenant."""
    return {"roles": roles}


@router.get("/viewer-role")
async def viewer_role(roles: list[str] = Depends(require_role(*CANONICAL_ROLES))):
    """Answer the roles of a caller who holds any canonical role in the request's tenant."""
    return {"roles": roles}
