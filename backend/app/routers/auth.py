from fastapi import APIRouter, Depends
from py_identity_model.identity import ClaimsPrincipal

from app.dependencies.auth import get_current_user

router = APIRouter()


@router.post("/auth/logout")
async def logout(principal: ClaimsPrincipal = Depends(get_current_user)):
    """Acknowledge a logout request from an authenticated user.

    The frontend is responsible for clearing tokens and redirecting.
    This endpoint validates the session is still active and signals
    that the backend is aware of the logout.
    """
    sub = None
    if principal.identity:
        for claim in principal.identity.claims:
            if claim.claim_type == "sub":
                sub = claim.value
                break
    return {"status": "logged_out", "sub": sub}
