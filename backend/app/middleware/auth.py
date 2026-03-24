from py_identity_model import TokenValidationConfig, to_principal
from py_identity_model.aio import validate_token
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.logging_config import get_logger

logger = get_logger(__name__)


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
            logger.info("auth.missing_header path=%s", request.url.path)
            cid = getattr(request.state, "correlation_id", None)
            body = {"detail": "Missing or invalid authorization header"}
            if cid:
                body["correlation_id"] = cid
            return JSONResponse(body, status_code=401)

        token = auth_header.removeprefix("Bearer ")

        try:
            config = TokenValidationConfig(
                perform_disco=True,
                audience=self.descope_project_id,
            )
            claims = await validate_token(
                jwt=token,
                token_validation_config=config,
                disco_doc_address=self.disco_address,
            )
            request.state.claims = claims
            request.state.principal = to_principal(claims, "Descope")
            request.state.tenant_id = claims.get("dct")
            logger.debug(
                "auth.token_validated sub=%s tenant=%s path=%s",
                claims.get("sub"),
                claims.get("dct"),
                request.url.path,
            )
        except Exception:
            logger.warning("auth.token_invalid path=%s", request.url.path)
            cid = getattr(request.state, "correlation_id", None)
            body = {"detail": "Invalid or expired token"}
            if cid:
                body["correlation_id"] = cid
            return JSONResponse(body, status_code=401)

        return await call_next(request)
