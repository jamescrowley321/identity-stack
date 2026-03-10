from fastapi import APIRouter, Depends

from app.dependencies.auth import get_current_user, get_claims
from py_identity_model.identity import ClaimsPrincipal

router = APIRouter()


@router.get("/me")
async def me(principal: ClaimsPrincipal = Depends(get_current_user)):
    """Return current user info from JWT claims."""
    return {
        "sub": principal.find_first("sub").value if principal.find_first("sub") else None,
        "email": principal.find_first("email").value if principal.find_first("email") else None,
        "name": principal.find_first("name").value if principal.find_first("name") else None,
    }


@router.get("/claims")
async def claims(claims: dict = Depends(get_claims)):
    """Return raw JWT claims (for debugging)."""
    return claims
