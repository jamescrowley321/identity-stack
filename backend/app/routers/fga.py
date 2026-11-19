from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.dependencies.capabilities import require_relationship_store
from app.dependencies.fga import (
    RelationshipCaller,
    get_relationship_caller,
    resolve_fga_user_target,
    store_error_response,
)
from app.dependencies.identity import get_relationship_store, get_user_directory, relationship_store_registration
from app.dependencies.rbac import require_role
from app.logbook import logbook
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.relationship_store import RelationshipStore, RelationshipStoreError
from app.services.relationship_users import UserDirectory

logger = logbook(__name__)

# Every route here reads or writes the deployment's relationship store. The gate
# serves each store only to the principals whose ids it holds (conventions §18):
# Descope's to Descope principals, OpenFGA's to principals authorized from the
# canonical model. Everyone else gets 404 before anything is read.
router = APIRouter(tags=["fga"], dependencies=[Depends(require_relationship_store)])


class UpdateSchemaRequest(BaseModel):
    # Generous limit for FGA schema bodies; prevents abuse without restricting real usage
    schema_: str = Field(alias="schema", min_length=1, max_length=50000)

    model_config = {"populate_by_name": True}


class RelationRequest(BaseModel):
    # FGA identifiers; short names/IDs — 200 is generous
    resource_type: str = Field(min_length=1, max_length=200)
    resource_id: str = Field(min_length=1, max_length=200)
    relation: str = Field(min_length=1, max_length=200)
    # A user identifier in any form the caller holds — user id, email, or username.
    # It is resolved to the user id the store keys on before it reaches the store,
    # because that is what every read keys on. See resolve_fga_user_target.
    target: str = Field(min_length=1, max_length=200)


# Nonexistent and outside the caller's tenant answer alike, so the answer is no existence oracle.
TARGET_NOT_FOUND = "Target user not found"


async def _target(directory: UserDirectory, body: RelationRequest, caller: RelationshipCaller) -> str | None:
    """The subject ``body.target`` names, resolved as the deployment's store requires (``open_subjects``)."""
    return await resolve_fga_user_target(
        directory,
        body.target,
        caller.tenant_id,
        open_subjects=relationship_store_registration().open_subjects,
    )


def _failed(exc: RelationshipStoreError, action: str) -> HTTPException:
    logger.warning("Relationship store error %s: %s %s", action, exc.kind, exc.upstream)
    return store_error_response(exc)


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
    except RelationshipStoreError as exc:
        raise _failed(exc, "loading the FGA schema") from exc


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
    except RelationshipStoreError as exc:
        # INVALID for a blank body: min_length=1 stops "" but not "   ".
        raise _failed(exc, "updating the FGA schema") from exc

    # Read-back is best-effort; if it fails, return the submitted schema.
    try:
        return {"schema": await store.read_schema() or body.schema_}
    except RelationshipStoreError as exc:
        logger.warning("FGA schema updated but read-back failed: %s %s", exc.kind, exc.upstream)
        return {"schema": body.schema_}


# --- Relation endpoints (tenant-scoped via resource_id prefixing) ---


@router.post("/fga/relations", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_relation(
    request: Request,
    body: RelationRequest,
    caller: RelationshipCaller = Depends(get_relationship_caller),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    store: RelationshipStore = Depends(get_relationship_store),
    directory: UserDirectory = Depends(get_user_directory),
):
    """Create an FGA relation tuple. Requires owner or admin role."""
    target = await _target(directory, body, caller)
    if target is None:
        raise HTTPException(status_code=404, detail=TARGET_NOT_FOUND)
    try:
        await store.write(caller.tenant_id, body.resource_type, body.resource_id, body.relation, target)
        return {
            "resource_type": body.resource_type,
            "resource_id": body.resource_id,
            "relation": body.relation,
            "target": target,
        }
    except RelationshipStoreError as exc:
        raise _failed(exc, "creating a relation") from exc


@router.delete("/fga/relations")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_relation(
    request: Request,
    body: RelationRequest,
    caller: RelationshipCaller = Depends(get_relationship_caller),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    store: RelationshipStore = Depends(get_relationship_store),
    directory: UserDirectory = Depends(get_user_directory),
):
    """Delete an FGA relation tuple. Requires owner or admin role."""
    target = await _target(directory, body, caller)
    if target is None:
        raise HTTPException(status_code=404, detail=TARGET_NOT_FOUND)
    try:
        await store.delete(caller.tenant_id, body.resource_type, body.resource_id, body.relation, target)
        return {"status": "deleted"}
    except RelationshipStoreError as exc:
        raise _failed(exc, "deleting a relation") from exc


@router.get("/fga/relations")
async def list_relations(
    request: Request,
    # FGA identifiers; Descope uses short names/IDs — 200 is generous
    resource_type: str = Query(min_length=1, max_length=200),
    resource_id: str = Query(min_length=1, max_length=200),
    relation: str | None = Query(default=None, min_length=1, max_length=200),
    caller: RelationshipCaller = Depends(get_relationship_caller),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    store: RelationshipStore = Depends(get_relationship_store),
):
    """List FGA relation tuples for a resource. Requires owner or admin role.

    With `relation`, answers "who holds this relation on this resource". Without it,
    returns every relation the schema defines for the resource type.
    """
    try:
        relations = await store.relations(caller.tenant_id, resource_type, resource_id, relation=relation)
        return {
            "relations": [
                {
                    "namespace": resource_type,
                    "resource": resource_id,
                    "relationDefinition": rel.relation,
                    "target": rel.subject,
                }
                for rel in relations
            ]
        }
    except RelationshipStoreError as exc:
        raise _failed(exc, "listing relations") from exc


@router.post("/fga/check")
@limiter.limit(RATE_LIMIT_AUTH)
async def check_permission(
    request: Request,
    body: RelationRequest,
    caller: RelationshipCaller = Depends(get_relationship_caller),
    _admin_roles: list[str] = Depends(require_role("owner", "admin")),
    store: RelationshipStore = Depends(get_relationship_store),
    directory: UserDirectory = Depends(get_user_directory),
):
    """Check an FGA permission. Requires owner or admin role. Fail-closed: errors deny access."""
    target = await _target(directory, body, caller)
    if target is None:
        # No user of the caller's tenant: it holds nothing here, and the store is not asked.
        return {"allowed": False}
    try:
        return {
            "allowed": await store.check(caller.tenant_id, body.resource_type, body.resource_id, body.relation, target)
        }
    except RelationshipStoreError as exc:
        raise _failed(exc, "checking a permission") from exc
