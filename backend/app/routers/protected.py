import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from py_identity_model import TokenValidationConfig
from py_identity_model.aio import validate_token
from py_identity_model.identity import ClaimsPrincipal

from app.dependencies.auth import get_claims, get_current_user
from app.dependencies.identity import (
    get_identity_provisioning_service,
    get_identity_resolution_service,
)
from app.errors.identity import NotFound, ValidationError
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService

router = APIRouter(tags=["Protected"])

DESCOPE_PROJECT_ID = os.getenv("DESCOPE_PROJECT_ID", "")
DISCO_ADDRESS = f"https://api.descope.com/{DESCOPE_PROJECT_ID}/.well-known/openid-configuration"

# Providers eligible for just-in-time provisioning on first login. Ory is
# greenfield (every user is JIT-provisioned); Descope users are pre-provisioned
# via the seed, so Descope is intentionally excluded by default.
JIT_ENABLED_PROVIDERS = {
    name.strip().lower() for name in os.getenv("JIT_ENABLED_PROVIDERS", "ory").split(",") if name.strip()
}


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


@router.get("/identity")
async def canonical_identity(
    request: Request,
    resolver: IdentityResolutionService = Depends(get_identity_resolution_service),
    provisioner: IdentityProvisioningService = Depends(get_identity_provisioning_service),
):
    """Return the canonical identity (user, roles, permissions, tenants) for the caller.

    Provider-neutral: roles/tenant come from the canonical model, not the token.
    For JIT-enabled providers (e.g. Ory), a first-login subject that resolves to no
    canonical user is provisioned on the fly, then resolved.
    """
    principal = getattr(request.state, "principal", None)
    claims = getattr(request.state, "claims", None)
    if principal is None or principal.identity is None or claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    provider_name = (principal.identity.authentication_type or "").lower()
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject")

    result = await resolver.resolve(provider=provider_name, sub=sub)
    if result.is_error() and provider_name in JIT_ENABLED_PROVIDERS:
        provisioned = await provisioner.provision(
            provider_name=provider_name,
            sub=sub,
            email=claims.get("email", ""),
            email_verified=bool(claims.get("email_verified", False)),
            given_name=claims.get("given_name", ""),
            family_name=claims.get("family_name", ""),
        )
        if provisioned.is_error():
            err = provisioned.error
            if isinstance(err, ValidationError):
                # Missing/unverified email in the token — a client-side problem.
                raise HTTPException(status_code=400, detail="A verified email is required to provision an identity")
            if isinstance(err, NotFound):
                # The provider isn't registered server-side — misconfiguration.
                raise HTTPException(status_code=500, detail="Identity provider not configured")
            raise HTTPException(status_code=409, detail="Could not provision identity")
        result = await resolver.resolve(provider=provider_name, sub=sub)

    if result.is_error():
        raise HTTPException(status_code=404, detail="Identity not found")
    return result.ok


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
