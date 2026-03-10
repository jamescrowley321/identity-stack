from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from py_identity_model import (
    DiscoveryDocumentRequest,
    TokenValidationConfig,
    to_principal,
)
from py_identity_model.aio import get_discovery_document, validate_token


class TokenValidationMiddleware(BaseHTTPMiddleware):
    """Validates Descope JWTs on protected routes using py-identity-model."""

    def __init__(self, app, descope_project_id: str, excluded_paths: set[str] | None = None):
        super().__init__(app)
        self.descope_project_id = descope_project_id
        self.excluded_paths = excluded_paths or set()
        self.disco_address = f"https://api.descope.com/{descope_project_id}/.well-known/openid-configuration"

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
                algorithms=["RS256"],
            )
            claims = await validate_token(
                jwt=token,
                token_validation_config=config,
                disco_doc_address=self.disco_address,
            )
            request.state.claims = claims
            request.state.principal = to_principal(claims, "Descope")
        except Exception:
            return JSONResponse({"detail": "Invalid or expired token"}, status_code=401)

        return await call_next(request)
