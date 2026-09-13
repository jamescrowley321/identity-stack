import logging
import re

import httpx
from fastapi import HTTPException, Request

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


async def resolve_fga_user_target(client, identifier: str) -> str:
    """Resolve a user identifier to the Descope userId FGA keys on, if it names one.

    Every FGA *read* in this service keys on the JWT ``sub``, which is a Descope
    userId: ``require_fga`` checks with it, and the owner tuple is written from
    it. A write that keys on whatever the caller happened to type therefore lands
    on a target no read will ever match. Sharing by email failed loudly (the ``@``
    is outside the FGA charset, so the client rejected it), but sharing by a
    charset-legal username returned 200 while granting nothing at all, and
    revoking reported success while removing nothing. Resolving caller-supplied
    targets through the same lookup makes both ends of the tuple agree.

    Best-effort by design: an identifier that names no Descope user is returned
    unchanged. ``/api/fga/relations`` and ``/api/fga/check`` are the raw admin
    surface over the authz store, where a target need not be a user at all — a
    permission check for a subject that does not exist must be answerable
    ("allowed: false"), not a 404. Endpoints that *require* the target to be a
    real user — document share and revoke — establish that themselves and answer
    404 before they get here.

    An upstream fault is never silently swallowed: only a clean "no such user"
    falls through to the identifier.
    """
    try:
        user = await client.load_user(identifier)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return identifier
        logger.error("Failed to resolve FGA target: HTTP %s", exc.response.status_code)
        raise HTTPException(status_code=502, detail="Failed to resolve target user") from exc
    except httpx.RequestError as exc:
        logger.error("Network error resolving FGA target: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Failed to resolve target user") from exc

    if isinstance(user, dict) and user.get("userId"):
        return str(user["userId"])
    return identifier


def require_fga(
    resource_type: str,
    relation: str,
    *,
    resource_id_param: str = "document_id",
    resource_id_pattern: str | None = None,
):
    """Dependency factory that enforces an FGA permission check.

    Extracts user_id from JWT claims (sub), tenant_id from JWT claims (dct),
    and resource_id from the path parameter named by ``resource_id_param``,
    then calls check_permission with a tenant-prefixed resource_id on the
    Descope Management API.

    Returns the caller's user_id for downstream use.
    Fail-closed: any FGA API error results in HTTP 502 (deny).

    ``resource_id_pattern`` is checked here rather than left to the path
    parameter's own ``Path(pattern=...)``. FastAPI solves dependencies before it
    validates the endpoint's own parameters, so a malformed id reached the FGA
    call first and came back 403 "denied" — Descope cannot hold a relation for a
    syntactically impossible resource — and the 422 the path parameter would have
    raised was never reported. Validating here makes the response describe the
    real fault and spares the upstream authz call.
    """
    compiled = re.compile(resource_id_pattern) if resource_id_pattern else None

    async def dependency(request: Request) -> str:
        user_id = extract_user_id(request)
        tenant_id = _extract_tenant_id(request)
        resource_id = request.path_params.get(resource_id_param, "")
        if not resource_id:
            raise HTTPException(status_code=400, detail="Missing resource identifier")
        if compiled is not None and not compiled.fullmatch(resource_id):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {resource_id_param}: expected {resource_id_pattern}",
            )
        prefixed_id = f"{tenant_id}:{resource_id}"
        try:
            client = request.app.state.descope_client
            allowed = await client.check_permission(resource_type, prefixed_id, relation, user_id)
        except httpx.HTTPStatusError as exc:
            # Include the upstream body. Without it a 400 here says only "the
            # authz store refused", and the reason — a relation the schema does
            # not define, a malformed identifier — is nowhere in the logs, which
            # is what made a drifted FGA schema undiagnosable from CI alone.
            logger.error(
                "FGA check failed (HTTP %s) user=%s resource=%s:%s relation=%s upstream=%s",
                exc.response.status_code,
                user_id,
                resource_type,
                resource_id,
                relation,
                exc.response.text[:500],
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
