from enum import Enum

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from py_identity_model import (
    ConfigurationException,
    DiscoveryValidationError,
    JwksValidationError,
    NetworkException,
    TokenValidationConfig,
)
from py_identity_model.aio import validate_token
from py_identity_model.identity import ClaimsPrincipal

from app.dependencies.auth import get_claims, get_current_user
from app.dependencies.identity import (
    get_identity_provisioning_service,
    get_identity_resolution_service,
)
from app.errors.identity import NotFound, ValidationError
from app.knobs import knob, knob_set
from app.logbook import logbook
from app.middleware.providers import descope_config
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.identity_provisioning import IdentityProvisioningService
from app.services.identity_resolution import IdentityResolutionService

router = APIRouter(tags=["Protected"])
logger = logbook(__name__)

DESCOPE_PROJECT_ID = knob("DESCOPE_PROJECT_ID", "")
# ``POST /validate-id-token`` runs behind neither token middleware, so it takes
# the Descope provider's discovery address, audience and signing algorithms
# from the same config they use.
DESCOPE = descope_config(DESCOPE_PROJECT_ID)

# What stops ``POST /validate-id-token`` before the token is judged: the
# provider's discovery document or JWKS cannot be fetched, was fetched and is
# unusable, or the provider config itself is refused. None of them says
# anything about the token, and none is a ``TokenValidationException``.
PROVIDER_FAULTS = (NetworkException, DiscoveryValidationError, JwksValidationError, ConfigurationException)
REFUSED_DETAIL = "Invalid or expired token"
UNAVAILABLE_DETAIL = "Identity provider unavailable"

# Providers eligible for just-in-time provisioning on first login. Ory is
# greenfield (every user is JIT-provisioned); Descope users are pre-provisioned
# via the seed, so Descope is intentionally excluded by default.
JIT_ENABLED_PROVIDERS = knob_set("JIT_ENABLED_PROVIDERS", "ory")


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


def _cause(exc: Exception) -> str:
    """What to log about a failed validation: the error's type and what the library typed.

    Never the message. The library quotes the token's header in it (``alg``,
    which the caller chooses), and this route answers anyone. ``kind`` is an
    enum member where the library sets one; ``status_code`` is the provider's
    HTTP status on a failed fetch.
    """
    name = type(exc).__name__
    kind = getattr(exc, "kind", None)
    if isinstance(kind, Enum):
        return f"{name} ({kind.value})"
    if isinstance(exc, NetworkException):
        return f"{name} (status {exc.status_code})"
    return name


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
        audience=DESCOPE.audience or "",
        algorithms=sorted(DESCOPE.algorithms) or None,
        require_https=DESCOPE.require_https,
    )
    try:
        return await validate_token(
            jwt=id_token,
            token_validation_config=config,
            disco_doc_address=DESCOPE.disco_address,
        )
    except PROVIDER_FAULTS as exc:
        logger.error("ID validation hit a provider fault: %s", _cause(exc))
        raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL) from exc
    except Exception as exc:
        # Anything else refuses the token, as in TokenValidationMiddleware, with
        # the body every token refusal gets. Not narrowed to
        # TokenValidationException: in discovery mode the library lets PyJWT's
        # DecodeError out for a token that is not a JWT (identity-model#1179).
        logger.warning("ID validation refused: %s", _cause(exc))
        raise HTTPException(status_code=401, detail=REFUSED_DETAIL) from exc
