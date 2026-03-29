from py_identity_model import TokenValidationConfig, to_principal
from py_identity_model.aio import validate_token
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class TokenValidationMiddleware(BaseHTTPMiddleware):
    """Validates Descope JWTs on protected routes using py-identity-model.

    Accepts both OIDC tokens (from client credentials flow) and Descope session
    JWTs (from access key exchange, OTP, etc.). These use different issuer formats:
      - OIDC: https://api.descope.com/{project_id}
      - Session: https://api.descope.com/v1/apps/{project_id}
    Both are signed by the same project JWKS keys.
    """

    def __init__(self, app, descope_project_id: str, excluded_paths: set[str] | None = None):
        super().__init__(app)
        self.descope_project_id = descope_project_id
        self.excluded_paths = excluded_paths or set()
        self.disco_address = f"https://api.descope.com/{descope_project_id}/.well-known/openid-configuration"
        self._accepted_issuers = frozenset(
            {
                f"https://api.descope.com/{descope_project_id}",
                f"https://api.descope.com/v1/apps/{descope_project_id}",
            }
        )

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.excluded_paths:
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse({"detail": "Missing or invalid authorization header"}, status_code=401)

        token = auth_header.removeprefix("Bearer ")

        try:
            config = TokenValidationConfig(
                perform_disco=True,
                audience=self.descope_project_id,
                # Disable PyJWT's strict issuer check; we validate against both
                # accepted Descope issuer formats manually below.
                options={"verify_iss": False},
            )
            claims = await validate_token(
                jwt=token,
                token_validation_config=config,
                disco_doc_address=self.disco_address,
            )
            # Validate issuer against accepted Descope formats when a
            # project ID is configured (in tests without DESCOPE_PROJECT_ID,
            # issuer validation is skipped since we can't determine the
            # expected value)
            if self.descope_project_id and "iss" in claims and claims["iss"] not in self._accepted_issuers:
                return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)
            request.state.claims = claims
            request.state.principal = to_principal(claims, "Descope")
            request.state.tenant_id = claims.get("dct")
        except Exception:
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

        return await call_next(request)
