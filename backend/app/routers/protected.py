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
from app.errors.identity import Forbidden, NotFound, ValidationError
from app.knobs import knob, knob_set
from app.logbook import logbook
from app.middleware.providers import ORGANIZATION_FIRST_LOGIN_PROVIDERS, ProviderTokenConfig, descope_config
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.first_login_profile import userinfo_profile
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
# First-login refusals of a provider whose organization picks the user's tenant (Zitadel, Keycloak).
ORGANIZATION_NOT_LINKED = "Your organization is not linked to a tenant"
USERINFO_SUBJECT_MISMATCH = "The identity provider's UserInfo does not match the token"
PROVIDER_NOT_CONFIGURED = "Identity provider not configured"
PROVISIONING_FAILED = "Could not provision identity"

# Providers eligible for just-in-time provisioning on first login. Ory is
# greenfield (every user is JIT-provisioned); Descope users are pre-provisioned
# via the seed, so Descope is intentionally excluded by default. Zitadel and
# Keycloak are opt-in (``ory,zitadel,keycloak``): their first-login users join
# the tenant their organization is linked to; Zitadel's take their email from
# UserInfo, Keycloak's from the access token.
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


def _recorded_provider(recorded: object, provider_name: str) -> ProviderTokenConfig | None:
    """The config the token middleware recorded for this principal's provider.

    Both middlewares record one beside the claims (conventions §18, and
    ``test_provider_registration.py::TestClaimsAreNeverRecordedWithoutTheirProvider``),
    so ``None`` is only a hand-built request, read as before: the profile from
    the token's claims. Not for a principal of a provider whose organization
    picks the first login's tenant (``ORGANIZATION_FIRST_LOGIN_PROVIDERS``):
    its first login needs its config's organization rules, so ``None`` is
    refused (500) for it, as is anything recorded that is not the principal's
    own provider config. A first login whose rules cannot be found is not
    provisioned.
    """
    if recorded is None and provider_name not in ORGANIZATION_FIRST_LOGIN_PROVIDERS:
        return None
    if not isinstance(recorded, ProviderTokenConfig) or recorded.name.lower() != provider_name:
        logger.error("first login refused: no provider config recorded for provider %s", provider_name)
        raise HTTPException(status_code=500, detail=PROVIDER_NOT_CONFIGURED)
    return recorded


async def _first_login_profile(
    config: ProviderTokenConfig | None,
    claims: dict,
    access_token: str,
    sub: str,
    provisioner: IdentityProvisioningService,
) -> dict:
    """The profile and organization ``provision`` is called with for a first-login subject.

    For a provider with a ``first_login_organization`` (Zitadel, Keycloak), a provider
    with no canonical ``providers`` row is refused (500), and a token that
    names no organization, or names one no single active tenant is linked to
    for that provider, is refused (403), all before UserInfo is fetched. The
    organization is passed on: provisioning checks the link again before it
    writes. For a provider
    with ``profile_from_userinfo``, the email and names come from UserInfo,
    never from the token: a UserInfo answer for another subject is refused
    (403) and one that cannot be had is 503, as every provider fault here is.
    Every other provider gets its profile from the token's claims, as before.
    """
    organization: str | None = None
    if config is not None and config.first_login_organization is not None:
        organization = config.first_login_organization(claims)
        if organization is None:
            logger.warning("first login refused: provider %s token names no organization", config.name)
            raise HTTPException(status_code=403, detail=ORGANIZATION_NOT_LINKED)
        linked = await provisioner.organization_tenant_id(config.name.lower(), organization)
        if linked.is_error() and isinstance(linked.error, NotFound):
            logger.error("first login refused: provider %s has no canonical provider row", config.name)
            raise HTTPException(status_code=500, detail=PROVIDER_NOT_CONFIGURED)
        if linked.is_error():
            logger.warning("first login refused: provider %s organization is linked to no single tenant", config.name)
            raise HTTPException(status_code=403, detail=ORGANIZATION_NOT_LINKED)
    if config is not None and config.profile_from_userinfo:
        fetched = await userinfo_profile(config, access_token, sub)
        if fetched.is_error():
            if isinstance(fetched.error, Forbidden):
                raise HTTPException(status_code=403, detail=USERINFO_SUBJECT_MISMATCH)
            raise HTTPException(status_code=503, detail=UNAVAILABLE_DETAIL)
        profile = {
            "email": fetched.ok.email,
            "email_verified": fetched.ok.email_verified,
            "given_name": fetched.ok.given_name,
            "family_name": fetched.ok.family_name,
        }
    else:
        profile = {
            "email": claims.get("email", ""),
            # Only the JSON boolean verifies an email: a string such as "false"
            # is truthy, and this flag is the email-linking guard's input.
            "email_verified": claims.get("email_verified") is True,
            "given_name": claims.get("given_name", ""),
            "family_name": claims.get("family_name", ""),
        }
    return profile if organization is None else {**profile, "organization_id": organization}


@router.get("/identity")
async def canonical_identity(
    request: Request,
    resolver: IdentityResolutionService = Depends(get_identity_resolution_service),
    provisioner: IdentityProvisioningService = Depends(get_identity_provisioning_service),
):
    """Return the canonical identity (user, roles, permissions, tenants) for the caller.

    Roles and tenants come from the canonical model. For JIT-enabled providers
    (``JIT_ENABLED_PROVIDERS``), a first-login subject that resolves to no
    canonical user is provisioned on the fly, then resolved: for Zitadel and
    Keycloak into the tenant linked to the token's organization, Zitadel's with
    the profile from UserInfo (``_first_login_profile``).
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
        first_login = await _first_login_profile(
            _recorded_provider(getattr(request.state, "provider", None), provider_name),
            claims,
            request.headers.get("Authorization", "").removeprefix("Bearer "),
            sub,
            provisioner,
        )
        provisioned = await provisioner.provision(
            provider_name=provider_name,
            sub=sub,
            **first_login,
            assign_default_role=True,
        )
        if provisioned.is_error():
            err = provisioned.error
            if isinstance(err, Forbidden):
                # The organization is linked to no single active tenant, or its
                # login was refused for its email (outside the tenant's domains,
                # or held): one answer, so which refusal fired is not disclosed.
                # Within the tenant's domains a free email gets 200, so this
                # 403 still says a canonical user holds the email.
                raise HTTPException(status_code=403, detail=ORGANIZATION_NOT_LINKED)
            if isinstance(err, ValidationError):
                # Missing/unverified email in the token — a client-side problem.
                raise HTTPException(status_code=400, detail="A verified email is required to provision an identity")
            if isinstance(err, NotFound):
                # The provider isn't registered server-side — misconfiguration.
                raise HTTPException(status_code=500, detail=PROVIDER_NOT_CONFIGURED)
            # EmailLinkRefused (another provider's first login with an email an
            # organization's login holds, logged by provisioning with the holder's
            # id) and every other conflict get the same body, naming no cause.
            raise HTTPException(status_code=409, detail=PROVISIONING_FAILED)
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
