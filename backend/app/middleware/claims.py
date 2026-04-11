import base64
import json
import logging
import time

from py_identity_model import to_principal
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

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

    Defense in depth: this middleware ALSO enforces ``exp`` and ``iss`` on
    every request (and ``aud`` when present), even though Tyk should have
    done the same. If Tyk is ever silently bypassed, misconfigured, or not
    in front of the backend — the exact failure mode that went undetected
    for four days while ``tyk/entrypoint.sh`` was broken (issue #240) —
    these checks prevent forged or expired tokens from being trusted.

    These checks are NOT a substitute for signature verification. If Tyk
    is not in front in production, a determined attacker can still forge a
    payload that satisfies ``exp``/``iss``/``aud``. Signature verification
    remains Tyk's responsibility; this middleware closes the "Tyk silently
    not running" gap, not the "Tyk running but compromised" gap.
    """

    def __init__(
        self,
        app,
        descope_project_id: str = "",
        excluded_paths: set[str] | None = None,
        excluded_prefixes: set[str] | None = None,
    ):
        super().__init__(app)
        self.descope_project_id = descope_project_id
        self.excluded_paths = excluded_paths or set()
        self.excluded_prefixes = tuple(excluded_prefixes) if excluded_prefixes else ()
        # Accepted issuers mirror TokenValidationMiddleware: Descope emits
        # two formats depending on the token type (OIDC vs session/access
        # key). Empty set means issuer validation is skipped, which is
        # only appropriate for tests without a configured project id.
        self._accepted_issuers = (
            frozenset(
                {
                    f"https://api.descope.com/{descope_project_id}",
                    f"https://api.descope.com/v1/apps/{descope_project_id}",
                }
            )
            if descope_project_id
            else frozenset()
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

            # Defense in depth: enforce issuer allow-list when a project
            # ID is configured. In tests without DESCOPE_PROJECT_ID the
            # check is skipped (mirrors TokenValidationMiddleware).
            if self._accepted_issuers:
                iss = claims.get("iss")
                if not isinstance(iss, str) or iss not in self._accepted_issuers:
                    logger.warning("GatewayClaims rejected: iss not in allow-list (iss=%r)", iss)
                    return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

            # Validate audience when present — OIDC tokens include aud,
            # but Descope session tokens (from access key exchange) do not.
            if self.descope_project_id and "aud" in claims:
                aud = claims["aud"]
                valid_aud = aud == self.descope_project_id or (isinstance(aud, list) and self.descope_project_id in aud)
                if not valid_aud:
                    logger.warning("GatewayClaims rejected: aud mismatch (aud=%r)", aud)
                    return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

            # For access key tokens: the exchange endpoint sets `tenants`
            # but not `dct` (current tenant).  When there is exactly one
            # tenant association, infer `dct` so downstream RBAC checks
            # (require_role / require_permission) work.
            if not claims.get("dct") and isinstance(claims.get("tenants"), dict):
                tenants = claims["tenants"]
                if len(tenants) == 1:
                    claims["dct"] = next(iter(tenants))

            request.state.claims = claims
            request.state.principal = to_principal(claims, "Descope")
            request.state.tenant_id = claims.get("dct")
        except Exception:
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

        return await call_next(request)
