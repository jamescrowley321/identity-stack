import logging

import httpx
from fastapi import HTTPException, Request

from app.services.descope import get_descope_client

logger = logging.getLogger(__name__)


def extract_user_id(request: Request) -> str:
    """Extract user_id from JWT claims on request.state.

    Raises 401 if claims are missing, not a dict, or lack a 'sub' field.
    Shared by require_fga dependency and document router endpoints.
    """
    claims = getattr(request.state, "claims", None)
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user identity")
    return user_id


def _extract_tenant_id(request: Request) -> str:
    """Extract tenant_id from JWT claims, mirroring get_tenant_id logic.

    This avoids calling the FastAPI dependency directly (which requires
    the dependency injection framework) while keeping the same validation.
    """
    claims = getattr(request.state, "claims", None)
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = claims.get("dct")
    if not tenant_id:
        raise HTTPException(status_code=403, detail="No tenant context")
    return tenant_id


def require_fga(
    resource_type: str,
    relation: str,
    *,
    resource_id_param: str = "document_id",
):
    """Dependency factory that enforces an FGA permission check.

    Extracts user_id from JWT claims (sub), tenant_id from JWT claims (dct),
    and resource_id from the path parameter named by ``resource_id_param``,
    then calls check_permission with a tenant-prefixed resource_id on the
    Descope Management API.

    Returns the caller's user_id for downstream use.
    Fail-closed: any FGA API error results in HTTP 502 (deny).
    """

    async def dependency(request: Request) -> str:
        user_id = extract_user_id(request)
        tenant_id = _extract_tenant_id(request)
        resource_id = request.path_params.get(resource_id_param, "")
        if not resource_id:
            raise HTTPException(status_code=400, detail="Missing resource identifier")
        prefixed_id = f"{tenant_id}:{resource_id}"
        try:
            client = get_descope_client()
            allowed = await client.check_permission(resource_type, prefixed_id, relation, user_id)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "FGA check failed (HTTP %s) user=%s resource=%s:%s relation=%s",
                exc.response.status_code,
                user_id,
                resource_type,
                resource_id,
                relation,
            )
            raise HTTPException(status_code=502, detail="Authorization check failed") from exc
        except httpx.RequestError as exc:
            logger.error(
                "FGA check network error user=%s resource=%s:%s relation=%s",
                user_id,
                resource_type,
                resource_id,
                relation,
            )
            raise HTTPException(status_code=502, detail="Authorization check failed") from exc
        if not allowed:
            logger.warning(
                "FGA denied: user=%s resource_type=%s resource_id=%s relation=%s",
                user_id,
                resource_type,
                resource_id,
                relation,
            )
            raise HTTPException(status_code=403, detail="Access denied")
        return user_id

    return dependency
