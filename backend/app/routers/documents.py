from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import select

from app.dependencies.capabilities import require_relationship_store
from app.dependencies.fga import (
    RelationshipCaller,
    find_user,
    get_relationship_caller,
    require_fga,
    store_error_response,
)
from app.dependencies.identity import get_relationship_store, get_user_directory
from app.dependencies.rbac import refuse_machine_principal
from app.dependencies.tenant import get_tenant_id
from app.logbook import logbook
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.models.database import get_async_session
from app.models.document import Document
from app.services.relationship_store import (
    Relation,
    RelationshipStore,
    RelationshipStoreError,
    RelationshipStoreErrorKind,
)
from app.services.relationship_users import UserDirectory

logger = logbook(__name__)

# Every route here keys relations on the request's tenant and the caller's subject
# id. The gate serves each store only to the principals whose ids it holds
# (conventions §18); everyone else gets 404 before anything is read.
router = APIRouter(tags=["documents"], dependencies=[Depends(require_relationship_store)])
# A share grants a person a relation, so no machine credential shares or revokes.
NO_MACHINES = [Depends(refuse_machine_principal)]

# Conservative limit to avoid SQLite SQLITE_MAX_VARIABLE_NUMBER (999)
_QUERY_BATCH_SIZE = 500
# Cap FGA relation cleanup to bound sequential deletes
_MAX_FGA_CLEANUP = 100

# UUID v4 pattern for document_id path parameter validation
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# Type alias for validated document_id path parameter
DocumentId = Annotated[str, Path(pattern=_UUID_PATTERN)]


class CreateDocumentRequest(BaseModel):
    # Standardized name/title limit; matches Descope's 255-char identifier limit
    title: str = Field(min_length=1, max_length=255)
    # Generous limit for document bodies; prevents abuse without restricting real usage
    content: str = Field(default="", max_length=50000)


class UpdateDocumentRequest(BaseModel):
    # Standardized name/title limit; matches Descope's 255-char identifier limit
    title: str | None = Field(default=None, min_length=1, max_length=255)
    # Generous limit for document bodies; prevents abuse without restricting real usage
    content: str | None = Field(default=None, max_length=50000)


class ShareDocumentRequest(BaseModel):
    # A user id or an email in the store's user directory; 200 is generous
    user_id: str = Field(min_length=1, max_length=200)
    relation: Literal["viewer", "editor"]


@router.post("/documents", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_document(
    request: Request,
    body: CreateDocumentRequest,
    caller: RelationshipCaller = Depends(get_relationship_caller),
    session: AsyncSession = Depends(get_async_session),
    store: RelationshipStore = Depends(get_relationship_store),
):
    """Create a document. FGA owner relation is created before DB commit (compensation on failure)."""
    tenant_id, user_id = caller.tenant_id, caller.user_id

    document = Document(
        tenant_id=tenant_id,
        title=body.title,
        content=body.content,
        created_by=user_id,
    )

    # FGA relation FIRST — if this fails, no DB row is created
    try:
        await store.write(tenant_id, "document", document.id, "owner", user_id)
    except RelationshipStoreError as exc:
        logger.error("Failed to create FGA owner relation for doc %s: %s %s", document.id, exc.kind, exc.upstream)
        raise store_error_response(exc) from exc

    # DB commit second — compensate FGA on failure
    try:
        session.add(document)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning("DB commit failed for doc %s, compensating FGA relation", document.id)
        try:
            await store.delete(tenant_id, "document", document.id, "owner", user_id)
        except Exception:
            logger.warning("FGA compensation failed for doc %s", document.id)
        raise HTTPException(status_code=500, detail="Failed to create document") from exc

    try:
        await session.refresh(document)
    except Exception:
        logger.warning("DB refresh failed for doc %s after successful commit", document.id)
        # commit succeeded, FGA relation exists — return the document as-is

    return document.model_dump()


async def _restore_relations(
    store: RelationshipStore, tenant_id: str, deleted_relations: list[Relation], document_id: str
) -> None:
    """Put back relations a failed delete had already removed.

    Best-effort: a compensation that itself fails is logged, not raised, because
    the caller is already on an error path and the original fault is the one
    worth reporting.
    """
    for dr in deleted_relations:
        try:
            await store.write(tenant_id, "document", document_id, dr.relation, dr.subject)
        except Exception:
            logger.warning(
                "FGA compensation failed for doc %s relation %s->%s",
                document_id,
                dr.relation,
                dr.subject,
            )


@router.get("/documents")
async def list_documents(
    request: Request,
    caller: RelationshipCaller = Depends(get_relationship_caller),
    session: AsyncSession = Depends(get_async_session),
    store: RelationshipStore = Depends(get_relationship_store),
):
    """List documents the caller can view, filtered by tenant."""
    tenant_id, user_id = caller.tenant_id, caller.user_id

    # The tenant's documents are the candidates: the store checks each, so none is
    # lost to a store's cap on how many objects it lists.
    candidates = (
        (await session.execute(select(Document.id).where(Document.tenant_id == tenant_id).order_by(Document.id)))
        .scalars()
        .all()
    )
    try:
        doc_ids = await store.objects(tenant_id, "document", "can_view", user_id, candidates=candidates)
    except RelationshipStoreError as exc:
        logger.error("FGA objects lookup failed for user %s: %s %s", user_id, exc.kind, exc.upstream)
        raise store_error_response(exc) from exc

    if not doc_ids:
        return {"documents": []}

    # Batch queries to avoid overly large IN clauses
    documents = []
    for i in range(0, len(doc_ids), _QUERY_BATCH_SIZE):
        batch = doc_ids[i : i + _QUERY_BATCH_SIZE]
        result = await session.execute(
            select(Document).where(
                Document.id.in_(batch),
                Document.tenant_id == tenant_id,
            )
        )
        docs = result.scalars().all()
        documents.extend(docs)

    return {"documents": [doc.model_dump() for doc in documents]}


@router.get("/documents/{document_id}")
async def get_document(
    document_id: DocumentId,
    user_id: str = Depends(require_fga("document", "can_view", resource_id_pattern=_UUID_PATTERN)),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
):
    """Get a single document. FGA can_view enforced via dependency."""
    document = await session.get(Document, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return document.model_dump()


@router.put("/documents/{document_id}")
@limiter.limit(RATE_LIMIT_AUTH)
async def update_document(
    request: Request,
    document_id: DocumentId,
    body: UpdateDocumentRequest,
    user_id: str = Depends(require_fga("document", "can_edit", resource_id_pattern=_UUID_PATTERN)),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
):
    """Update a document. FGA can_edit enforced via dependency."""
    document = await session.get(Document, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if body.title is None and body.content is None:
        return document.model_dump()
    if body.title is not None:
        document.title = body.title
    if body.content is not None:
        document.content = body.content
    try:
        session.add(document)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.error("DB commit failed for doc %s update: %s", document_id, type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to update document") from exc

    try:
        await session.refresh(document)
    except Exception:
        logger.warning("DB refresh failed for doc %s after successful update commit", document_id)

    return document.model_dump()


@router.delete("/documents/{document_id}")
@limiter.limit(RATE_LIMIT_AUTH)
async def delete_document(
    request: Request,
    document_id: DocumentId,
    user_id: str = Depends(require_fga("document", "can_delete", resource_id_pattern=_UUID_PATTERN)),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
    store: RelationshipStore = Depends(get_relationship_store),
):
    """Delete a document. FGA relations cleaned up first; abort on FGA failure."""
    document = await session.get(Document, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # FGA cleanup first — abort if it fails or if too many relations
    try:
        # Every relation on the document, so cleanup deletes all of them.
        relations = await store.all_relations(tenant_id, "document", document_id)
    except RelationshipStoreError as exc:
        logger.error("FGA cleanup failed for doc %s: %s %s", document_id, exc.kind, exc.upstream)
        raise store_error_response(exc) from exc

    if len(relations) > _MAX_FGA_CLEANUP:
        raise HTTPException(
            status_code=409,
            detail=f"Document has {len(relations)} relations (max {_MAX_FGA_CLEANUP}). Remove shares before deleting.",
        )

    # Store relations for compensation before deleting them
    deleted_relations: list[Relation] = []
    try:
        for rel in relations:
            await store.delete(tenant_id, "document", document_id, rel.relation, rel.subject)
            deleted_relations.append(rel)
    except RelationshipStoreError as exc:
        # A failure partway through leaves the document with a partially stripped
        # ACL: the caller sees the error, the row is still there, and the owner is
        # locked out of a document nothing can restore. Put back what this request
        # removed before reporting the failure — the same compensation the
        # DB-failure path below has always done.
        logger.error("FGA cleanup failed for doc %s: %s %s", document_id, exc.kind, exc.upstream)
        await _restore_relations(store, tenant_id, deleted_relations, document_id)
        raise store_error_response(exc) from exc

    # DB delete — compensate FGA on failure by re-creating deleted relations
    try:
        await session.delete(document)
        await session.commit()
    except StaleDataError:
        # Two concurrent DELETEs: the other request already removed the row. The
        # document IS gone, so this is a success, not a DB fault — compensating
        # here would re-create every relation for a document that no longer
        # exists, leaving orphan grants in the authz store. DELETE is idempotent.
        await session.rollback()
        logger.info("Doc %s was deleted by a concurrent request", document_id)
        return {"status": "deleted", "id": document_id}
    except Exception as exc:
        await session.rollback()
        logger.error(
            "DB delete failed for doc %s after FGA cleanup — attempting compensation",
            document_id,
        )
        await _restore_relations(store, tenant_id, deleted_relations, document_id)
        raise HTTPException(status_code=500, detail="Failed to delete document") from exc

    return {"status": "deleted", "id": document_id}


@router.post("/documents/{document_id}/share", dependencies=NO_MACHINES)
@limiter.limit(RATE_LIMIT_AUTH)
async def share_document(
    request: Request,
    document_id: DocumentId,
    body: ShareDocumentRequest,
    user_id: str = Depends(require_fga("document", "owner", resource_id_pattern=_UUID_PATTERN)),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
    store: RelationshipStore = Depends(get_relationship_store),
    directory: UserDirectory = Depends(get_user_directory),
):
    """Share a document with another user. Only the owner can share."""
    document = await session.get(Document, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Prevent self-sharing (owner already has access)
    if body.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot share a document with yourself")

    # Verify target user exists and is in the same tenant, in the store's own directory
    target_user = await find_user(directory, body.user_id)
    if target_user is None:
        raise HTTPException(status_code=404, detail="Target user not found")
    if tenant_id not in target_user.tenant_ids:
        raise HTTPException(
            status_code=403,
            detail="Cannot share with users outside your tenant",
        )

    # The tuple must key on the user id every read uses, not on whatever
    # identifier the caller addressed the person by. See resolve_fga_user_target.
    target_user_id = target_user.user_id
    if target_user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot share a document with yourself")

    try:
        await store.write(tenant_id, "document", document_id, body.relation, target_user_id)
    except RelationshipStoreError as exc:
        logger.error("Failed to create share relation for doc %s: %s %s", document_id, exc.kind, exc.upstream)
        raise store_error_response(exc) from exc

    return {
        "document_id": document_id,
        "user_id": target_user_id,
        "relation": body.relation,
    }


@router.delete("/documents/{document_id}/share/{target_user_id}", dependencies=NO_MACHINES)
@limiter.limit(RATE_LIMIT_AUTH)
async def revoke_share(
    request: Request,
    document_id: DocumentId,
    target_user_id: str,
    user_id: str = Depends(require_fga("document", "owner", resource_id_pattern=_UUID_PATTERN)),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
    store: RelationshipStore = Depends(get_relationship_store),
    directory: UserDirectory = Depends(get_user_directory),
):
    """Revoke a user's access to a document. Deletes both viewer and editor relations."""
    document = await session.get(Document, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Verify target user exists and is in the same tenant
    target_user = await find_user(directory, target_user_id)
    if target_user is None:
        raise HTTPException(status_code=404, detail="Target user not found")
    if tenant_id not in target_user.tenant_ids:
        raise HTTPException(
            status_code=403,
            detail="Cannot revoke access for users outside your tenant",
        )
    resolved_target_id = target_user.user_id

    for relation in ("viewer", "editor"):
        try:
            await store.delete(tenant_id, "document", document_id, relation, resolved_target_id)
        except RelationshipStoreError as exc:
            # The relation may not be there: a store that says so has nothing to revoke.
            if exc.kind in (RelationshipStoreErrorKind.REFUSED, RelationshipStoreErrorKind.ABSENT):
                continue
            logger.error(
                "Failed to revoke %s for user %s on doc %s: %s %s",
                relation,
                target_user_id,
                document_id,
                exc.kind,
                exc.upstream,
            )
            raise HTTPException(status_code=502, detail="Failed to revoke document access") from exc

    return {
        "status": "revoked",
        "document_id": document_id,
        "user_id": resolved_target_id,
    }
