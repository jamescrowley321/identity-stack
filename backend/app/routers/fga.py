import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fga"])


class UpdateSchemaRequest(BaseModel):
    # Generous limit for FGA schema bodies; prevents abuse without restricting real usage
    schema_: str = Field(alias="schema", min_length=1, max_length=50000)

    model_config = {"populate_by_name": True}


class RelationRequest(BaseModel):
    # FGA identifiers; Descope uses short names/IDs — 200 is generous
    resource_type: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    relation: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)


def _prefix_resource_id(tenant_id: str, resource_id: str) -> str:
    """Prefix a resource ID with the tenant ID for FGA tenant isolation."""
    return f"{tenant_id}:{resource_id}"


def _strip_tenant_prefix(tenant_id: str, resource_id: str) -> str:
    """Strip the tenant prefix from a resource ID before returning to client."""
    prefix = f"{tenant_id}:"
    return resource_id[len(prefix) :] if resource_id.startswith(prefix) else resource_id


def _sanitize_error_detail(response_text: str) -> str:
    """Extract a safe error message from Descope API error responses."""
    try:
        import json

        data = json.loads(response_text)
        if isinstance(data, dict) and "message" in data:
            return f"Validation error: {data['message'][:200]}"
    except (json.JSONDecodeError, TypeError):
        pass
    return f"Validation error: {response_text[:200]}"


# --- Schema endpoints (project-global, no tenant prefixing) ---


@router.get("/fga/schema")
async def get_fga_schema(
    request: Request,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Get the current FGA schema. Requires owner or admin role.

    Note: FGA schema is project-global and affects all tenants.
    """
    try:
        client = request.app.state.descope_client
        schema = await client.get_fga_schema() or {}
        # get_fga_schema() already extracts .schema from the API response,
        # so `schema` is the schema dict itself (with .namespaces etc.),
        # not a wrapper around it.
        return {"schema": schema}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error loading FGA schema: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
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
    """Update the FGA schema. Requires owner or admin role.

    Note: FGA schema is project-global and affects all tenants.
    """
    try:
        client = request.app.state.descope_client
        await client.update_fga_schema(body.schema_)
    except httpx.HTTPStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error updating FGA schema: %s %s", exc.response.status_code, resp_body)
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
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


# --- Relation endpoints (tenant-scoped via resource_id prefixing) ---


@router.post("/fga/relations", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_relation(
    request: Request,
    body: RelationRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Create an FGA relation tuple. Requires owner or admin role."""
    prefixed_id = _prefix_resource_id(tenant_id, body.resource_id)
    try:
        client = request.app.state.descope_client
        await client.create_relation(body.resource_type, prefixed_id, body.relation, body.target)
        return {
            "resource_type": body.resource_type,
            "resource_id": body.resource_id,
            "relation": body.relation,
            "target": body.target,
        }
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error creating relation: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to create relation in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error creating relation: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.delete("/fga/relations")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_relation(
    request: Request,
    body: RelationRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Delete an FGA relation tuple. Requires owner or admin role."""
    prefixed_id = _prefix_resource_id(tenant_id, body.resource_id)
    try:
        client = request.app.state.descope_client
        await client.delete_relation(body.resource_type, prefixed_id, body.relation, body.target)
        return {"status": "deleted"}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error deleting relation: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to delete relation in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error deleting relation: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.get("/fga/relations")
async def list_relations(
    request: Request,
    # FGA identifiers; Descope uses short names/IDs — 200 is generous
    resource_type: str = Query(min_length=1, max_length=200),
    resource_id: str = Query(min_length=1, max_length=200),
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List FGA relation tuples for a resource. Requires owner or admin role."""
    prefixed_id = _prefix_resource_id(tenant_id, resource_id)
    try:
        client = request.app.state.descope_client
        relations = await client.list_relations(resource_type, prefixed_id) or []
        # Strip tenant prefix from resource_id in response items
        for rel in relations:
            if isinstance(rel, dict) and "resource" in rel:
                rel["resource"] = _strip_tenant_prefix(tenant_id, rel["resource"])
            if isinstance(rel, dict) and "resource_id" in rel:
                rel["resource_id"] = _strip_tenant_prefix(tenant_id, rel["resource_id"])
        return {"relations": relations}
    except httpx.HTTPStatusError as exc:
        logger.warning("Descope API error listing relations: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to list relations from Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error listing relations: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.post("/fga/check")
@limiter.limit(RATE_LIMIT_AUTH)
async def check_permission(
    request: Request,
    body: RelationRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """Check an FGA permission. Requires owner or admin role. Fail-closed: errors deny access."""
    prefixed_id = _prefix_resource_id(tenant_id, body.resource_id)
    try:
        client = request.app.state.descope_client
        allowed = bool(await client.check_permission(body.resource_type, prefixed_id, body.relation, body.target))
        return {"allowed": allowed}
    except httpx.HTTPStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error checking permission: %s %s", exc.response.status_code, resp_body)
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to check permission in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error checking permission: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc
