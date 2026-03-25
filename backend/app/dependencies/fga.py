from fastapi import HTTPException, Request

from app.logging_config import get_logger
from app.services.fga import get_fga_client

logger = get_logger(__name__)


def require_fga(resource_type: str, relation: str):
    """Dependency factory that checks FGA permission before allowing access.

    Extracts user_id from JWT claims and document_id from the path parameter.
    Returns the FGA client for downstream use.
    """

    async def dependency(request: Request, document_id: str):
        claims = getattr(request.state, "claims", None)
        if claims is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user_id = claims.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Missing user identity")

        fga = get_fga_client()
        allowed = await fga.check_permission(resource_type, document_id, relation, user_id)
        if not allowed:
            logger.warning(
                "fga.denied user=%s resource=%s:%s relation=%s",
                user_id,
                resource_type,
                document_id,
                relation,
            )
            raise HTTPException(status_code=403, detail="Access denied")
        return fga

    return dependency
