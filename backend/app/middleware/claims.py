import base64
import json
import logging

from py_identity_model import to_principal
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class GatewayClaimsMiddleware(BaseHTTPMiddleware):
    """Extracts JWT claims without signature verification for gateway mode.

    In gateway mode, Tyk has already validated the JWT signature, expiry,
    and issuer. This middleware base64-decodes the payload to populate
    request.state.claims, request.state.principal, and request.state.tenant_id
    so that downstream RBAC dependencies (require_role / require_permission)
    work identically to standalone mode.
    """

    def __init__(
        self,
        app,
        excluded_paths: set[str] | None = None,
        excluded_prefixes: set[str] | None = None,
    ):
        super().__init__(app)
        self.excluded_paths = excluded_paths or set()
        self.excluded_prefixes = tuple(excluded_prefixes) if excluded_prefixes else ()

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
        except (ValueError, json.JSONDecodeError):
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

        return await call_next(request)
