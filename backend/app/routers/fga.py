import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.dependencies.rbac import require_role
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.descope import get_descope_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fga"])


class UpdateSchemaRequest(BaseModel):
    schema_: str = Field(alias="schema", min_length=1, max_length=50000)

    model_config = {"populate_by_name": True}


class RelationRequest(BaseModel):
    resource_type: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    relation: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)


@router.get("/fga/schema")
async def get_fga_schema(
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Get the current FGA schema. Requires owner or admin role."""
    try:
        client = get_descope_client()
        result = await client.get_fga_schema() or {}
        return {"schema": result.get("schema") or ""}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error loading FGA schema: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=exc.response.text[:500]) from exc
        raise HTTPException(status_code=502, detail="Failed to load FGA schema from Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error loading FGA schema: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.put("/fga/schema")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_fga_schema(
    request: Request,
    body: UpdateSchemaRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Update the FGA schema. Requires owner or admin role."""
    try:
        client = get_descope_client()
        await client.update_fga_schema(body.schema_)
    except httpx.HTTPStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error updating FGA schema: %s %s", exc.response.status_code, resp_body)
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=exc.response.text[:500]) from exc
        raise HTTPException(status_code=502, detail="Failed to update FGA schema in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error updating FGA schema: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc

    # Read-back is best-effort; if it fails, return the submitted schema
    try:
        result = await client.get_fga_schema() or {}
        return {"schema": result.get("schema") or ""}
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("FGA schema updated but read-back failed: %s", exc)
        return {"schema": body.schema_}


@router.post("/fga/relations", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_relation(
    request: Request,
    body: RelationRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Create an FGA relation tuple. Requires owner or admin role."""
    try:
        client = get_descope_client()
        await client.create_relation(body.resource_type, body.resource_id, body.relation, body.target)
        return {
            "resource_type": body.resource_type,
            "resource_id": body.resource_id,
            "relation": body.relation,
            "target": body.target,
        }
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error creating relation: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=exc.response.text[:500]) from exc
        raise HTTPException(status_code=502, detail="Failed to create relation in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error creating relation: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.delete("/fga/relations")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_relation(
    request: Request,
    body: RelationRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Delete an FGA relation tuple. Requires owner or admin role."""
    try:
        client = get_descope_client()
        await client.delete_relation(body.resource_type, body.resource_id, body.relation, body.target)
        return {"status": "deleted"}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error deleting relation: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=exc.response.text[:500]) from exc
        raise HTTPException(status_code=502, detail="Failed to delete relation in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error deleting relation: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.get("/fga/relations")
async def list_relations(
    resource_type: str = Query(min_length=1, max_length=200),
    resource_id: str = Query(min_length=1, max_length=200),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List FGA relation tuples for a resource. Requires owner or admin role."""
    try:
        client = get_descope_client()
        relations = await client.list_relations(resource_type, resource_id) or []
        return {"relations": relations}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error listing relations: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=exc.response.text[:500]) from exc
        raise HTTPException(status_code=502, detail="Failed to list relations from Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error listing relations: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.post("/fga/check")
async def check_permission(
    body: RelationRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Check an FGA permission. Requires owner or admin role. Fail-closed: errors deny access."""
    try:
        client = get_descope_client()
        allowed = bool(await client.check_permission(body.resource_type, body.resource_id, body.relation, body.target))
        return {"allowed": allowed}
    except httpx.HTTPStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error checking permission: %s %s", exc.response.status_code, resp_body)
        raise HTTPException(status_code=502, detail="Failed to check permission in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error checking permission: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc
