from fastapi import Request, HTTPException
from py_identity_model.identity import ClaimsPrincipal


def get_current_user(request: Request) -> ClaimsPrincipal:
    """Extract the authenticated user's ClaimsPrincipal from the request."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return principal


def get_claims(request: Request) -> dict:
    """Extract raw JWT claims from the request."""
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return claims
