from fastapi import HTTPException, Request


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
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user_roles

    return dependency


def require_permission(*permissions: str):
    """Dependency factory that enforces the user has one of the specified permissions
    in their current tenant (from the Descope `dct` + `tenants` JWT claims).

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
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user_permissions

    return dependency
