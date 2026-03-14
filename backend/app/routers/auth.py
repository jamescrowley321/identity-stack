import os

import httpx
from fastapi import APIRouter, Depends

from app.dependencies.auth import get_claims

router = APIRouter()

DESCOPE_BASE_URL = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")


@router.post("/auth/logout")
async def logout(claims: dict = Depends(get_claims)):
    """Log out the current user by revoking all their Descope sessions."""
    project_id = os.environ["DESCOPE_PROJECT_ID"]
    management_key = os.getenv("DESCOPE_MANAGEMENT_KEY", "")
    user_id = claims.get("sub")

    if user_id and management_key:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{DESCOPE_BASE_URL}/v1/mgmt/user/logout",
                headers={"Authorization": f"Bearer {project_id}:{management_key}"},
                json={"userId": user_id},
            )

    return {"status": "logged_out", "sub": user_id}
