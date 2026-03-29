import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from py_identity_model import TokenValidationConfig
from py_identity_model.aio import validate_token
from py_identity_model.identity import ClaimsPrincipal

from app.dependencies.auth import get_claims, get_current_user
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter

router = APIRouter(tags=["Protected"])

DESCOPE_PROJECT_ID = os.getenv("DESCOPE_PROJECT_ID", "")
DISCO_ADDRESS = f"https://api.descope.com/{DESCOPE_PROJECT_ID}/.well-known/openid-configuration"


def _serialize_principal(principal: ClaimsPrincipal) -> dict:
    """Serialize a ClaimsPrincipal to a JSON-friendly dict."""
    identity = principal.identity
    return {
        "identity": {
            "authentication_type": identity.authentication_type if identity else None,
            "is_authenticated": identity.is_authenticated() if identity else False,
            "name": identity.name if identity else None,
            "claims": [
                {"type": claim.claim_type, "value": claim.value, "issuer": claim.issuer}
                for claim in (identity.claims if identity else [])
            ],
        },
    }


@router.get("/me")
async def me(principal: ClaimsPrincipal = Depends(get_current_user)):
    """Return the ClaimsIdentity from py-identity-model."""
    return _serialize_principal(principal)


@router.get("/claims")
async def claims(claims: dict = Depends(get_claims)):
    """Return raw access token claims validated by py-identity-model."""
    return claims


@router.post("/validate-id-token")
@limiter.limit(RATE_LIMIT_AUTH)
async def validate_id_token(request: Request, authorization: str = Header()):
    """Validate an ID token server-side and return its claims."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Invalid authorization header")

    id_token = authorization.removeprefix("Bearer ")

    config = TokenValidationConfig(
        perform_disco=True,
        audience=DESCOPE_PROJECT_ID,
    )
    id_claims = await validate_token(
        jwt=id_token,
        token_validation_config=config,
        disco_doc_address=DISCO_ADDRESS,
    )
    return id_claims
