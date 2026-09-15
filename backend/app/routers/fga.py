import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.dependencies.fga import resolve_fga_user_target
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
    # A user identifier in any form the caller holds — userId, email, or username.
    # It is resolved to the Descope userId before it reaches FGA, because that is
    # what every read keys on. See resolve_fga_user_target.
    target: str = Field(min_length=1, max_length=200)


def _prefix_resource_id(tenant_id: str, resource_id: str) -> str:
    """Prefix a resource ID with the tenant ID for FGA tenant isolation."""
    return f"{tenant_id}:{resource_id}"


def _strip_tenant_prefix(tenant_id: str, resource_id: object) -> object:
    """Strip the tenant prefix from a resource ID before returning to client.

    Non-string values are returned untouched: a null ``resource`` in an upstream
    relation reached ``.startswith`` and raised AttributeError, which Starlette
    turned into a 500 for a response the caller could otherwise have read.
    """
    if not isinstance(resource_id, str):
        return resource_id
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
        # The AuthZ 1.0 DSL string — the same text infra/fga.tf declares and the
        # same text PUT takes back, so the round trip this page performs is
        # symmetric. The UI already expected a string (FGAManagement.tsx renders
        # it into the editor); it was handed a namespaces object instead.
        schema = await client.get_fga_schema() or ""
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
    except ValueError as exc:
        # The client rejects an empty/blank schema with ValueError. min_length=1
        # stops "" but not "   ", so this fifth route was the one that still let a
        # blank body through to an unhandled 500 — the other four already guard it.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error updating FGA schema: %s %s", exc.response.status_code, resp_body)
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to update FGA schema in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error updating FGA schema: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc

    # Read-back is best-effort; if it fails, return the submitted schema.
    # get_fga_schema returns the DSL string, not a wrapper dict — reading a key
    # off it raised AttributeError and this route answered 500 on a save that
    # had in fact succeeded.
    try:
        return {"schema": await client.get_fga_schema() or body.schema_}
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
    client = request.app.state.descope_client
    target = await resolve_fga_user_target(client, body.target, tenant_id)
    try:
        await client.create_relation(body.resource_type, prefixed_id, body.relation, target)
        return {
            "resource_type": body.resource_type,
            "resource_id": body.resource_id,
            "relation": body.relation,
            "target": target,
        }
    except json.JSONDecodeError as exc:
        # Must precede ValueError: JSONDecodeError subclasses it, and an upstream body
        # we cannot parse is an upstream fault, not the caller's malformed request.
        logger.warning("Descope returned an unparseable body: %s", exc)
        raise HTTPException(status_code=502, detail="Descope returned an unparseable response") from exc
    except ValueError as exc:
        # The client validates FGA identifiers itself and raises ValueError. That is a
        # malformed request, not a server fault — uncaught it escaped the handler and
        # Starlette turned it into an unhandled 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    client = request.app.state.descope_client
    target = await resolve_fga_user_target(client, body.target, tenant_id)
    try:
        await client.delete_relation(body.resource_type, prefixed_id, body.relation, target)
        return {"status": "deleted"}
    except json.JSONDecodeError as exc:
        # Must precede ValueError: JSONDecodeError subclasses it, and an upstream body
        # we cannot parse is an upstream fault, not the caller's malformed request.
        logger.warning("Descope returned an unparseable body: %s", exc)
        raise HTTPException(status_code=502, detail="Descope returned an unparseable response") from exc
    except ValueError as exc:
        # The client validates FGA identifiers itself and raises ValueError. That is a
        # malformed request, not a server fault — uncaught it escaped the handler and
        # Starlette turned it into an unhandled 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    relation: str | None = Query(default=None, min_length=1, max_length=200),
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
):
    """List FGA relation tuples for a resource. Requires owner or admin role.

    With `relation`, answers "who holds this relation on this resource". Without it,
    returns every relation the schema defines for the resource type.
    """
    prefixed_id = _prefix_resource_id(tenant_id, resource_id)
    try:
        client = request.app.state.descope_client
        relations = await client.list_relations(resource_type, prefixed_id, relation=relation) or []
        # Strip tenant prefix from resource_id in response items
        for rel in relations:
            if isinstance(rel, dict) and "resource" in rel:
                rel["resource"] = _strip_tenant_prefix(tenant_id, rel["resource"])
            if isinstance(rel, dict) and "resource_id" in rel:
                rel["resource_id"] = _strip_tenant_prefix(tenant_id, rel["resource_id"])
        return {"relations": relations}
    except json.JSONDecodeError as exc:
        # Must precede ValueError: JSONDecodeError subclasses it, and an upstream body
        # we cannot parse is an upstream fault, not the caller's malformed request.
        logger.warning("Descope returned an unparseable body: %s", exc)
        raise HTTPException(status_code=502, detail="Descope returned an unparseable response") from exc
    except ValueError as exc:
        # The client validates FGA identifiers itself and raises ValueError. That is a
        # malformed request, not a server fault — uncaught it escaped the handler and
        # Starlette turned it into an unhandled 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    client = request.app.state.descope_client
    target = await resolve_fga_user_target(client, body.target, tenant_id)
    try:
        allowed = bool(await client.check_permission(body.resource_type, prefixed_id, body.relation, target))
        return {"allowed": allowed}
    except json.JSONDecodeError as exc:
        # Must precede ValueError: JSONDecodeError subclasses it, and an upstream body
        # we cannot parse is an upstream fault, not the caller's malformed request.
        logger.warning("Descope returned an unparseable body: %s", exc)
        raise HTTPException(status_code=502, detail="Descope returned an unparseable response") from exc
    except ValueError as exc:
        # The client validates FGA identifiers itself and raises ValueError. That is a
        # malformed request, not a server fault — uncaught it escaped the handler and
        # Starlette turned it into an unhandled 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error checking permission: %s %s", exc.response.status_code, resp_body)
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to check permission in Descope") from exc
    except httpx.RequestError as exc:
        logger.error("Network error checking permission: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc
