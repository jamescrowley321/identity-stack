import logging

from py_identity_model import TokenValidationConfig, to_principal
from py_identity_model.aio import validate_token
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.middleware.providers import (
    audience_rejected,
    build_provider_configs,
    infer_single_tenant_dct,
    order_candidates,
    unverified_issuer,
)

logger = logging.getLogger(__name__)


class TokenValidationMiddleware(BaseHTTPMiddleware):
    """Validates OIDC JWTs on protected routes using py-identity-model.

    Provider-agnostic: the accepted issuers, discovery address, audience, and
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

    Descope specifics preserved: two issuer formats (OIDC vs session/access-key),
    ``aud`` omitted on access-key tokens, and single-tenant ``dct`` inference.
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
    ):
        super().__init__(app)
        self.excluded_paths = excluded_paths or set()
        self.excluded_prefixes = tuple(excluded_prefixes) if excluded_prefixes else ()
        self._providers = build_provider_configs(
            descope_project_id=descope_project_id,
            ory_issuer_url=ory_issuer_url,
            ory_audience=ory_audience,
            ory_require_audience=ory_require_audience,
        )

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.excluded_paths or request.url.path.startswith(self.excluded_prefixes):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse({"detail": "Missing or invalid authorization header"}, status_code=401)

        token = auth_header.removeprefix("Bearer ")

        try:
            iss_hint = unverified_issuer(token)
            for provider in order_candidates(self._providers, iss_hint):
                # Validate the signature against this provider's JWKS. Issuer and
                # audience are checked manually below (Descope disables the library
                # checks because session tokens use a non-discovery issuer and omit
                # aud); Ory could use the library checks, but the manual path is
                # uniform and equivalent.
                config = TokenValidationConfig(
                    perform_disco=True,
                    audience=provider.audience or "",
                    options={"verify_iss": False, "verify_aud": False},
                )
                try:
                    claims = await validate_token(
                        jwt=token,
                        token_validation_config=config,
                        disco_doc_address=provider.disco_address,
                    )
                except Exception:
                    # Wrong provider (signature verified against the wrong JWKS) or
                    # an invalid token — try the next configured provider.
                    logger.debug("token did not validate against provider %s; trying next", provider.name)
                    continue

                # Issuer allow-list: when the token carries an ``iss`` that this
                # provider does not accept, it belongs to a different provider —
                # try the next. (Absent ``iss`` keeps the historical lenient path.)
                if provider.accepted_issuers and "iss" in claims and claims["iss"] not in provider.accepted_issuers:
                    continue

                # Audience: fail-closed for providers that require it (Ory); for
                # Descope, checked only when present (session tokens omit ``aud``).
                if audience_rejected(claims, provider):
                    return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

                infer_single_tenant_dct(claims, provider)
                request.state.claims = claims
                request.state.principal = to_principal(claims, provider.name)
                request.state.tenant_id = claims.get("dct")
                return await call_next(request)

            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
        except Exception:
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
