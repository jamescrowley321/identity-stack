from fastapi import HTTPException, Request

from app.logging_config import get_logger

logger = get_logger(__name__)


def require_role(*roles: str):
    """Dependency factory that enforces the user has one of the specified roles
    in their current tenant (from the Descope `dct` + `tenants` JWT claims).

    Returns the user's role list for downstream use.
    """

    def dependency(request: Request) -> list[str]:
        claims = getattr(request.state, "claims", None)
        if claims is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        tenant_id = claims.get("dct")
        if not tenant_id:
            raise HTTPException(status_code=403, detail="No tenant context")
        tenant_info = claims.get("tenants", {}).get(tenant_id, {})
        user_roles = tenant_info.get("roles", []) if isinstance(tenant_info, dict) else []
        if not any(r in user_roles for r in roles):
            logger.warning(
                "rbac.role_denied sub=%s tenant=%s required=%s actual=%s",
                claims.get("sub"),
                tenant_id,
                roles,
                user_roles,
            )
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user_roles

    return dependency


def require_permission(*permissions: str):
    """Dependency factory that enforces the user has at least one of the specified
    permissions in their current tenant (from the Descope `dct` + `tenants` JWT claims).

    Alias: ``require_any_permission``.

    Returns the user's permission list for downstream use.
    """

    def dependency(request: Request) -> list[str]:
        claims = getattr(request.state, "claims", None)
        if claims is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        tenant_id = claims.get("dct")
        if not tenant_id:
            raise HTTPException(status_code=403, detail="No tenant context")
        tenant_info = claims.get("tenants", {}).get(tenant_id, {})
        user_permissions = tenant_info.get("permissions", []) if isinstance(tenant_info, dict) else []
        if not any(p in user_permissions for p in permissions):
            logger.warning(
                "rbac.permission_denied sub=%s tenant=%s required=%s",
                claims.get("sub"),
                tenant_id,
                permissions,
            )
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user_permissions

    return dependency


# Backward-compatible alias
require_any_permission = require_permission


def require_all_permissions(*permissions: str):
    """Dependency factory that enforces the user has ALL of the specified permissions
    in their current tenant (from the Descope `dct` + `tenants` JWT claims).

    Unlike ``require_permission`` (any-of), this requires every listed permission.

    Returns the user's permission list for downstream use.
    """

    def dependency(request: Request) -> list[str]:
        claims = getattr(request.state, "claims", None)
        if claims is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        tenant_id = claims.get("dct")
        if not tenant_id:
            raise HTTPException(status_code=403, detail="No tenant context")
        tenant_info = claims.get("tenants", {}).get(tenant_id, {})
        user_permissions = tenant_info.get("permissions", []) if isinstance(tenant_info, dict) else []
        missing = [p for p in permissions if p not in user_permissions]
        if missing:
            logger.warning(
                "rbac.all_permissions_denied sub=%s tenant=%s missing=%s",
                claims.get("sub"),
                tenant_id,
                missing,
            )
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user_permissions

    return dependency
