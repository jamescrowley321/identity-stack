import os

import httpx
from fastapi import APIRouter, Depends, Request

from app.dependencies.auth import get_claims
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter

router = APIRouter()

DESCOPE_BASE_URL = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")


@router.post("/auth/logout")
@limiter.limit(RATE_LIMIT_AUTH)
async def logout(request: Request, claims: dict = Depends(get_claims)):
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


# Known OAuth provider identifiers that may appear in Descope amr claims
_OAUTH_PROVIDERS = {"google", "github", "apple", "facebook", "microsoft", "gitlab", "discord", "linkedin", "slack"}


def _detect_auth_method(claims: dict) -> dict:
    """Derive authentication method and provider from JWT claims.

    Descope includes an ``amr`` (Authentication Methods References) list in the
    JWT.  Values include ``"pwd"`` for password, ``"otp"`` for one-time
    password, ``"mfa"`` for multi-factor, and OAuth provider names like
    ``"google"`` or ``"github"``.
    """
    amr = claims.get("amr", [])
    if not isinstance(amr, list):
        amr = []

    # Detect provider from amr entries
    provider = None
    for entry in amr:
        if isinstance(entry, str) and entry.lower() in _OAUTH_PROVIDERS:
            provider = entry.lower()
            break

    # Determine method category
    if provider:
        method = "oauth"
    elif "pwd" in amr:
        method = "password"
    elif "otp" in amr:
        method = "otp"
    elif "mfa" in amr:
        method = "mfa"
    elif "webauthn" in amr:
        method = "passkey"
    elif "magiclink" in amr:
        method = "magiclink"
    else:
        method = "unknown"

    return {"method": method, "provider": provider, "amr": amr}


@router.get("/auth/method")
async def auth_method(claims: dict = Depends(get_claims)):
    """Return the authentication method used for the current session.

    Inspects the ``amr`` JWT claim to detect whether the user logged in
    via social OAuth (Google, GitHub, etc.), password, passkey (WebAuthn),
    OTP, or another method.
    """
    return _detect_auth_method(claims)
