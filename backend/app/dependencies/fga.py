import logging

import httpx
from fastapi import HTTPException, Request

from app.services.descope import get_descope_client

logger = logging.getLogger(__name__)


def require_fga(resource_type: str, relation: str):
    """Dependency factory that enforces an FGA permission check.

    Extracts user_id from JWT claims (sub) and resource_id from the
    ``document_id`` path parameter, then calls check_permission on the
    Descope Management API.

    Returns the caller's user_id for downstream use.
    Fail-closed: any FGA API error results in HTTP 502 (deny).
    """

    async def dependency(request: Request, document_id: str) -> str:
        claims = getattr(request.state, "claims", None)
        if claims is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user_id = claims.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Missing user identity")
        try:
            client = get_descope_client()
            allowed = await client.check_permission(resource_type, document_id, relation, user_id)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "FGA check failed (HTTP %s) user=%s resource=%s:%s relation=%s",
                exc.response.status_code,
                user_id,
                resource_type,
                document_id,
                relation,
            )
            raise HTTPException(status_code=502, detail="Authorization check failed") from exc
        except httpx.RequestError as exc:
            logger.error(
                "FGA check failed (network) user=%s resource=%s:%s relation=%s: %s",
                user_id,
                resource_type,
                document_id,
                relation,
                exc,
            )
            raise HTTPException(status_code=502, detail="Authorization check failed") from exc
        if not allowed:
            logger.warning(
                "FGA denied: user=%s resource_type=%s resource_id=%s relation=%s",
                user_id,
                resource_type,
                document_id,
                relation,
            )
            raise HTTPException(status_code=403, detail="Access denied")
        return user_id

    return dependency
