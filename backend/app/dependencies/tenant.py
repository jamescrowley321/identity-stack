from fastapi import HTTPException, Request


def get_tenant_id(request: Request) -> str:
    """Extract the current tenant ID from the Descope `dct` JWT claim.

    Raises 403 if the user has no active tenant context.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = claims.get("dct")
    if not tenant_id:
        raise HTTPException(status_code=403, detail="No tenant context — select a tenant first")
    return tenant_id


def get_tenant_claims(request: Request) -> dict:
    """Return the `tenants` claim mapping from the JWT.

    Returns an empty dict if the user has no tenant memberships.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return claims.get("tenants", {})
