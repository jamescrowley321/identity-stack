from fastapi import APIRouter, Depends, Request

from app.dependencies.tenant import get_tenant_claims, get_tenant_id

router = APIRouter()

# Static hierarchy definition — mirrors infra/rbac.tf
ROLE_HIERARCHY = {
    "owner": {
        "description": "Full access including billing",
        "permissions": [
            "projects.create",
            "projects.read",
            "projects.update",
            "projects.delete",
            "members.invite",
            "members.remove",
            "members.update_role",
            "documents.read",
            "documents.write",
            "documents.delete",
            "settings.manage",
            "billing.manage",
        ],
    },
    "admin": {
        "description": "Full access except billing",
        "permissions": [
            "projects.create",
            "projects.read",
            "projects.update",
            "projects.delete",
            "members.invite",
            "members.remove",
            "members.update_role",
            "documents.read",
            "documents.write",
            "documents.delete",
            "settings.manage",
        ],
    },
    "member": {
        "description": "Standard team member with read/write access (default role)",
        "permissions": [
            "projects.read",
            "projects.update",
            "members.invite",
            "documents.read",
            "documents.write",
        ],
    },
    "viewer": {
        "description": "Read-only access",
        "permissions": [
            "projects.read",
            "documents.read",
        ],
    },
}


@router.get("/rbac/hierarchy")
async def get_hierarchy():
    """Return the role hierarchy with permissions each role grants.

    No authentication required — informational endpoint.
    """
    return {"roles": ROLE_HIERARCHY}


@router.get("/rbac/effective")
async def get_effective_permissions(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    tenant_claims: dict = Depends(get_tenant_claims),
):
    """Return the current user's effective roles and permissions in their active tenant."""
    tenant_info = tenant_claims.get(tenant_id, {})
    if not isinstance(tenant_info, dict):
        tenant_info = {}
    return {
        "user_id": getattr(request.state, "claims", {}).get("sub"),
        "tenant_id": tenant_id,
        "roles": tenant_info.get("roles", []),
        "permissions": tenant_info.get("permissions", []),
    }


@router.get("/rbac/check/{permission}")
async def check_permission(
    permission: str,
    tenant_id: str = Depends(get_tenant_id),
    tenant_claims: dict = Depends(get_tenant_claims),
):
    """Check if the current user has a specific permission in their active tenant."""
    tenant_info = tenant_claims.get(tenant_id, {})
    if not isinstance(tenant_info, dict):
        tenant_info = {}
    user_permissions = tenant_info.get("permissions", [])
    return {
        "permission": permission,
        "allowed": permission in user_permissions,
    }
