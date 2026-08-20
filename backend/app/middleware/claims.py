import base64
import json
import logging
import time

from py_identity_model import to_principal
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.middleware.providers import (
    audience_ok,
    build_provider_configs,
    infer_single_tenant_dct,
    select_by_issuer,
)

logger = logging.getLogger(__name__)

# Allow 30s of clock skew when validating exp — matches typical JWT library defaults.
_EXP_LEEWAY_SECONDS = 30


class GatewayClaimsMiddleware(BaseHTTPMiddleware):
    """Extracts JWT claims without signature verification for gateway mode.

    Primary enforcement in gateway mode is Tyk, which validates the JWT
    signature, expiry, issuer, and audience before forwarding the request.
    This middleware base64-decodes the pre-validated payload to populate
    request.state.claims, request.state.principal, and request.state.tenant_id
    so that downstream RBAC dependencies (require_role / require_permission)
    work identically to standalone mode.

    Provider-agnostic: the accepted issuers, audience, and claim handling come
    from the configured provider list (see ``app.middleware.providers``). Descope
    stays a configured provider; Ory is added by configuration.

    Defense in depth: this middleware ALSO enforces ``exp`` and ``iss`` on
    every request (and ``aud`` when the provider defines one), even though Tyk
    should have done the same. If Tyk is ever silently bypassed, misconfigured,
    or not in front of the backend — the failure mode that went undetected for
    four days while ``tyk/entrypoint.sh`` was broken (issue #240) — these checks
    prevent forged or expired tokens from being trusted.

    These checks are NOT a substitute for signature verification. If Tyk is not
    in front in production, a determined attacker can still forge a payload that
    satisfies ``exp``/``iss``/``aud``. Signature verification remains Tyk's
    responsibility; this middleware closes the "Tyk silently not running" gap,
    not the "Tyk running but compromised" gap.
    """

    def __init__(
        self,
        app,
        descope_project_id: str = "",
        excluded_paths: set[str] | None = None,
        excluded_prefixes: set[str] | None = None,
        ory_issuer_url: str = "",
        ory_audience: str | None = None,
    ):
        super().__init__(app)
        self.excluded_paths = excluded_paths or set()
        self.excluded_prefixes = tuple(excluded_prefixes) if excluded_prefixes else ()
        self._providers = build_provider_configs(
            descope_project_id=descope_project_id,
            ory_issuer_url=ory_issuer_url,
            ory_audience=ory_audience,
        )

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.excluded_paths or request.url.path.startswith(self.excluded_prefixes):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse({"detail": "Missing or invalid authorization header"}, status_code=401)

        token = auth_header.removeprefix("Bearer ")

        try:
            parts = token.split(".")
            if len(parts) != 3:
                return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

            # Base64-decode the payload (second segment)
            payload_b64 = parts[1]
            # Add padding if needed
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding

            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            claims = json.loads(payload_bytes)

            if not isinstance(claims, dict):
                return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

            # Defense in depth: enforce exp even though Tyk should have.
            # `bool` is a subclass of `int` in Python, so rule it out explicitly
            # to avoid accepting `{"exp": True}` as a valid numeric expiry.
            exp = claims.get("exp")
            if not isinstance(exp, (int, float)) or isinstance(exp, bool):
                logger.warning("GatewayClaims rejected: exp claim missing or non-numeric")
                return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
            if exp <= time.time() - _EXP_LEEWAY_SECONDS:
                logger.warning("GatewayClaims rejected: exp claim past leeway (exp=%s)", exp)
                return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

            # Select the provider by the token's issuer. When no provider enforces
            # an issuer allow-list (no project id / no Ory configured), this returns
            # the default provider and issuer validation is skipped — the historical
            # behavior. Otherwise an unknown/missing issuer is rejected.
            provider = select_by_issuer(self._providers, claims.get("iss"))
            if provider is None:
                logger.warning("GatewayClaims rejected: iss not in allow-list (iss=%r)", claims.get("iss"))
                return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

            # Validate audience when the provider defines one and the token carries
            # it — OIDC tokens include aud, but Descope session tokens do not.
            if provider.audience and "aud" in claims and not audience_ok(claims["aud"], provider.audience):
                logger.warning("GatewayClaims rejected: aud mismatch (aud=%r)", claims["aud"])
                return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

            # Descope access-key tokens set `tenants` but not `dct`; infer the
            # current tenant when there is exactly one. No-op for Ory.
            infer_single_tenant_dct(claims, provider)

            request.state.claims = claims
            request.state.principal = to_principal(claims, provider.name)
            request.state.tenant_id = claims.get("dct")
        except Exception:
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

        return await call_next(request)
