import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.dependencies.capabilities import require_relationship_store
from app.dependencies.descope import require_descope_principal
from app.dependencies.fga import resolve_fga_user_target
from app.dependencies.identity import get_relationship_store
from app.dependencies.rbac import require_role
from app.dependencies.tenant import get_tenant_id
from app.logbook import logbook
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.outbound import UpstreamStatusError, UpstreamTransportError
from app.services.relationship_store import RelationshipStore

logger = logbook(__name__)

# Every route here reads or writes Descope's FGA store, so each serves Descope principals
# only (conventions §18). The schema belongs to the whole Descope project: without the
# guard an admin of any one canonical tenant would replace it for every tenant.
router = APIRouter(tags=["fga"], dependencies=[Depends(require_relationship_store), Depends(require_descope_principal)])


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
    store: RelationshipStore = Depends(get_relationship_store),
):
    """Get the current FGA schema. Requires owner or admin role.

    Note: FGA schema is project-global and affects all tenants.
    """
    try:
        # The same text PUT takes back, so the round trip this page performs is
        # symmetric. The UI expects a string (FGAManagement.tsx renders it into
        # the editor).
        return {"schema": await store.read_schema()}
    except UpstreamStatusError as exc:
        logger.warning("Descope API error loading FGA schema: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to load FGA schema from Descope") from exc
    except UpstreamTransportError as exc:
        logger.error("Network error loading FGA schema: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.put("/fga/schema")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_fga_schema(
    request: Request,
    body: UpdateSchemaRequest,
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    store: RelationshipStore = Depends(get_relationship_store),
):
    """Update the FGA schema. Requires owner or admin role.

    Note: FGA schema is project-global and affects all tenants.
    """
    try:
        await store.write_schema(body.schema_)
    except ValueError as exc:
        # The client rejects an empty/blank schema with ValueError. min_length=1
        # stops "" but not "   ", so this fifth route was the one that still let a
        # blank body through to an unhandled 500 — the other four already guard it.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UpstreamStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error updating FGA schema: %s %s", exc.response.status_code, resp_body)
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to update FGA schema in Descope") from exc
    except UpstreamTransportError as exc:
        logger.error("Network error updating FGA schema: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc

    # Read-back is best-effort; if it fails, return the submitted schema.
    try:
        return {"schema": await store.read_schema() or body.schema_}
    except (UpstreamStatusError, UpstreamTransportError) as exc:
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
    store: RelationshipStore = Depends(get_relationship_store),
):
    """Create an FGA relation tuple. Requires owner or admin role."""
    target = await resolve_fga_user_target(request.app.state.descope_client, body.target, tenant_id)
    try:
        await store.write(tenant_id, body.resource_type, body.resource_id, body.relation, target)
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
    except UpstreamStatusError as exc:
        logger.warning("Descope API error creating relation: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to create relation in Descope") from exc
    except UpstreamTransportError as exc:
        logger.error("Network error creating relation: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.delete("/fga/relations")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_relation(
    request: Request,
    body: RelationRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    store: RelationshipStore = Depends(get_relationship_store),
):
    """Delete an FGA relation tuple. Requires owner or admin role."""
    target = await resolve_fga_user_target(request.app.state.descope_client, body.target, tenant_id)
    try:
        await store.delete(tenant_id, body.resource_type, body.resource_id, body.relation, target)
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
    except UpstreamStatusError as exc:
        logger.warning("Descope API error deleting relation: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to delete relation in Descope") from exc
    except UpstreamTransportError as exc:
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
    store: RelationshipStore = Depends(get_relationship_store),
):
    """List FGA relation tuples for a resource. Requires owner or admin role.

    With `relation`, answers "who holds this relation on this resource". Without it,
    returns every relation the schema defines for the resource type.
    """
    try:
        return {"relations": await store.relations(tenant_id, resource_type, resource_id, relation=relation)}
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
    except UpstreamStatusError as exc:
        logger.warning("Descope API error listing relations: %s %s", exc.response.status_code, exc.response.text[:500])
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to list relations from Descope") from exc
    except UpstreamTransportError as exc:
        logger.error("Network error listing relations: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc


@router.post("/fga/check")
@limiter.limit(RATE_LIMIT_AUTH)
async def check_permission(
    request: Request,
    body: RelationRequest,
    tenant_id: str = Depends(get_tenant_id),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    store: RelationshipStore = Depends(get_relationship_store),
):
    """Check an FGA permission. Requires owner or admin role. Fail-closed: errors deny access."""
    target = await resolve_fga_user_target(request.app.state.descope_client, body.target, tenant_id)
    try:
        return {"allowed": await store.check(tenant_id, body.resource_type, body.resource_id, body.relation, target)}
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
    except UpstreamStatusError as exc:
        resp_body = exc.response.text[:500]
        logger.warning("Descope API error checking permission: %s %s", exc.response.status_code, resp_body)
        if exc.response.status_code == 400:
            raise HTTPException(status_code=400, detail=_sanitize_error_detail(exc.response.text)) from exc
        raise HTTPException(status_code=502, detail="Failed to check permission in Descope") from exc
    except UpstreamTransportError as exc:
        logger.error("Network error checking permission: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to reach Descope API") from exc
