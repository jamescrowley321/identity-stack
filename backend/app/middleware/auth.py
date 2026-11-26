from typing import NamedTuple

from py_identity_model import TokenValidationConfig, to_principal
from py_identity_model.aio import validate_token
from py_identity_model.identity import ClaimsPrincipal
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.logbook import logbook
from app.middleware.providers import (
    ProviderTokenConfig,
    audience_rejected,
    binding_refusal,
    build_provider_configs,
    claimed_tenant_id,
    infer_single_tenant_dct,
    library_token_issuers,
    order_candidates,
    token_kind_rejected,
    unverified_issuer,
)

logger = logbook(__name__)


class _Authenticated(NamedTuple):
    """Everything authentication puts on ``request.state``, built before any of it is set."""

    claims: dict
    provider: ProviderTokenConfig
    principal: ClaimsPrincipal
    tenant_id: str | None


class TokenValidationMiddleware(BaseHTTPMiddleware):
    """Validates OIDC JWTs on protected routes using py-identity-model.

    Provider-agnostic: the accepted issuers, discovery issuer, audience, and
    claim handling come from the configured provider list (see
    ``app.middleware.providers``) rather than being hardcoded to Descope. Descope
    stays a configured provider; Ory (or any OIDC provider) is added by
    configuration, not new validation code.

    For each request the middleware selects a provider by the token's issuer,
    validates the signature against **that provider's** JWKS, then enforces the
    provider's issuer allow-list and audience. Selecting the JWKS by the token's
    (unverified) issuer is safe because the signature is always verified against
    the selected provider's keys — a token claiming another issuer but signed with
    a foreign key fails verification.

    Descope specifics preserved: three issuer formats (two OIDC URL forms plus the
    bare project id used by session/access-key tokens), ``aud`` omitted on
    access-key tokens, and single-tenant ``dct`` inference.
    Ory tokens are standard OIDC and carry no ``dct``/``tenants``.
    """

    def __init__(
        self,
        app,
        descope_project_id: str = "",
        excluded_paths: set[str] | None = None,
        excluded_prefixes: set[str] | None = None,
        ory_issuer_url: str = "",
        ory_audience: str | None = None,
        ory_require_audience: bool = True,
        providers: list[ProviderTokenConfig] | None = None,
    ):
        super().__init__(app)
        self.excluded_paths = excluded_paths or set()
        self.excluded_prefixes = tuple(excluded_prefixes) if excluded_prefixes else ()
        # ``providers`` is the list from ``configured_providers``. The Descope and
        # Ory arguments are for callers that build the middleware by hand.
        self._providers = (
            list(providers)
            if providers is not None
            else build_provider_configs(
                descope_project_id=descope_project_id,
                ory_issuer_url=ory_issuer_url,
                ory_audience=ory_audience,
                ory_require_audience=ory_require_audience,
            )
        )

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.excluded_paths or request.url.path.startswith(self.excluded_prefixes):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse({"detail": "Missing or invalid authorization header"}, status_code=401)

        token = auth_header.removeprefix("Bearer ")

        # Authentication is the ONLY thing guarded by this try, and ALL of it is:
        # validating the token, building the principal and reading the tenant
        # with the provider's extractor happen inside ``_authenticate``, so a
        # failure in any of them is the uniform 401, as it is in gateway mode.
        # ``call_next`` is deliberately outside it: a downstream failure
        # (database, upstream provider, application bug) must surface as its own
        # status code, not be relabelled "Invalid or expired token". Wrapping the
        # request handling in the authentication except-clause reported every
        # unhandled 500 as a 401 and made real outages read as auth failures.
        try:
            authenticated = await self._authenticate(token)
        except Exception:
            # WARNING, not DEBUG: this is the terminal rejection, not one of the
            # per-candidate misses below. At DEBUG a production 401 left no
            # evidence of why a token was refused, so an outage and a bad token
            # were indistinguishable from the logs.
            logger.warning("token authentication failed", exc_info=True)
            authenticated = None

        if authenticated is None:
            # Reached either from the except above or from _authenticate
            # exhausting every candidate provider without raising, which
            # previously logged nothing at all.
            logger.warning("rejecting request: no configured provider accepted the token")
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

        request.state.claims = authenticated.claims
        request.state.principal = authenticated.principal
        # The provider is recorded for the tenant resolver, which reads the
        # tenant and the grants with this provider's own extractor (conventions §18).
        request.state.provider = authenticated.provider
        request.state.tenant_id = authenticated.tenant_id
        return await call_next(request)

    async def _authenticate(self, token: str) -> _Authenticated | None:
        """Validate ``token`` against the configured providers.

        Returns the accepted claims, the provider that accepted them, and the
        principal and the tenant read from them, or None when no configured
        provider accepts the token. Raises when the principal cannot be built or
        the provider's claims extractor fails: the caller answers both with 401.
        """
        iss_hint = unverified_issuer(token)
        for provider in order_candidates(self._providers, iss_hint):
            # Validate the signature against this provider's JWKS. The library
            # always checks ``iss`` in discovery mode: against the discovery
            # issuer, or against ``token_issuers`` for a provider that signs
            # several issuer values with one key set (Descope; see
            # ``library_token_issuers``). The audience is checked below
            # (Descope session tokens omit ``aud``), so none is passed and the
            # library's check is off. ``algorithms`` is the provider's
            # allowlist, which the library enforces on the token's header
            # ``alg``; None accepts the library's default asymmetric algorithms.
            config = TokenValidationConfig(
                perform_disco=True,
                algorithms=sorted(provider.algorithms) or None,
                token_issuers=library_token_issuers(provider),
                options={"verify_aud": False},
                require_https=provider.require_https,
            )
            try:
                claims = await validate_token(
                    jwt=token,
                    token_validation_config=config,
                    issuer=provider.discovery_issuer,
                )
            except Exception:
                # Wrong provider (signature verified against the wrong JWKS) or
                # an invalid token — try the next configured provider.
                logger.debug("validation failed for candidate provider %s; trying next", provider.name)
                continue

            # Issuer allow-list: when the token carries an ``iss`` that this
            # provider does not accept, it belongs to a different provider —
            # try the next. (Absent ``iss`` keeps the historical lenient path.)
            if provider.accepted_issuers and "iss" in claims and claims["iss"] not in provider.accepted_issuers:
                continue

            # Audience: fail-closed for providers that require it (Ory); for
            # Descope, checked only when present (session tokens omit ``aud``).
            if audience_rejected(claims, provider):
                return None

            # Token binding: what the provider requires of a token beyond its
            # audience (Keycloak and Zitadel: issued to the SPA, and an access
            # token). Checked here so that it holds on every route.
            unbound = binding_refusal(claims, provider)
            if unbound is not None:
                logger.warning("rejected: %s binding refused (%s)", provider.name, unbound)
                return None

            # Token kind: a bare-project-id token must be a session token. The
            # audience check above cannot reject these — they carry no ``aud``.
            if token_kind_rejected(claims, provider):
                logger.debug("rejected: token kind not accepted on the bare issuer (drn=%r)", claims.get("drn"))
                return None

            infer_single_tenant_dct(claims, provider)
            return _Authenticated(
                claims=claims,
                provider=provider,
                principal=to_principal(claims, provider.name),
                tenant_id=claimed_tenant_id(claims, provider),
            )

        return None
