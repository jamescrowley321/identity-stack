import logging
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dependencies.fga import extract_user_id, require_fga
from app.dependencies.tenant import get_tenant_id
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.models.database import get_async_session
from app.models.document import Document
from app.services.descope import get_descope_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

# Conservative limit to avoid SQLite SQLITE_MAX_VARIABLE_NUMBER (999)
_QUERY_BATCH_SIZE = 500
# Cap FGA relation cleanup to bound sequential deletes
_MAX_FGA_CLEANUP = 100

# UUID v4 pattern for document_id path parameter validation
_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

# Type alias for validated document_id path parameter
DocumentId = Annotated[str, Path(pattern=_UUID_PATTERN)]


def _prefix_resource_id(tenant_id: str, resource_id: str) -> str:
    """Prefix a resource ID with the tenant ID for FGA tenant isolation."""
    return f"{tenant_id}:{resource_id}"


class CreateDocumentRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=50000)


class UpdateDocumentRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, max_length=50000)


class ShareDocumentRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    relation: Literal["viewer", "editor"]


@router.post("/documents", status_code=201)
@limiter.limit(RATE_LIMIT_AUTH)
async def create_document(
    request: Request,
    body: CreateDocumentRequest,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
):
    """Create a document. FGA owner relation is created before DB commit (compensation on failure)."""
    user_id = extract_user_id(request)

    document = Document(
        tenant_id=tenant_id,
        title=body.title,
        content=body.content,
        created_by=user_id,
    )

    # FGA relation FIRST — if this fails, no DB row is created
    prefixed_id = _prefix_resource_id(tenant_id, document.id)
    try:
        client = get_descope_client()
        await client.create_relation("document", prefixed_id, "owner", user_id)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error(
            "Failed to create FGA owner relation for doc %s: %s",
            document.id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to set document permissions") from exc

    # DB commit second — compensate FGA on failure
    try:
        session.add(document)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning("DB commit failed for doc %s, compensating FGA relation", document.id)
        try:
            await client.delete_relation("document", prefixed_id, "owner", user_id)
        except Exception:
            logger.warning("FGA compensation failed for doc %s", document.id)
        raise HTTPException(status_code=500, detail="Failed to create document") from exc

    try:
        await session.refresh(document)
    except Exception:
        logger.warning("DB refresh failed for doc %s after successful commit", document.id)
        # commit succeeded, FGA relation exists — return the document as-is

    return document.model_dump()


@router.get("/documents")
async def list_documents(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
):
    """List documents the caller can view, filtered by tenant."""
    user_id = extract_user_id(request)

    try:
        client = get_descope_client()
        resources = await client.list_user_resources("document", "can_view", user_id)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error(
            "FGA list_user_resources failed for user %s: %s",
            user_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to check document permissions") from exc

    resources = resources or []
    # Strip tenant prefix from resource IDs returned by FGA
    tenant_prefix = f"{tenant_id}:"
    doc_ids = []
    for r in resources:
        if isinstance(r, dict) and r.get("resource"):
            rid = r["resource"]
            if rid.startswith(tenant_prefix):
                doc_ids.append(rid[len(tenant_prefix) :])
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
    user_id: str = Depends(require_fga("document", "can_view")),
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
    user_id: str = Depends(require_fga("document", "can_edit")),
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
    user_id: str = Depends(require_fga("document", "can_delete")),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
):
    """Delete a document. FGA relations cleaned up first; abort on FGA failure."""
    document = await session.get(Document, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    prefixed_id = _prefix_resource_id(tenant_id, document_id)

    # FGA cleanup first — abort if it fails or if too many relations
    try:
        client = get_descope_client()
        relations = (await client.list_relations("document", prefixed_id)) or []
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error("FGA cleanup failed for doc %s: %s", document_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Failed to clean up document permissions") from exc

    if len(relations) > _MAX_FGA_CLEANUP:
        raise HTTPException(
            status_code=409,
            detail=f"Document has {len(relations)} relations (max {_MAX_FGA_CLEANUP}). Remove shares before deleting.",
        )

    # Store relations for compensation before deleting them
    deleted_relations: list[dict] = []
    try:
        for rel in relations:
            rd = rel.get("relationDefinition", "") if isinstance(rel, dict) else ""
            target = rel.get("target", "") if isinstance(rel, dict) else ""
            if rd and target:
                await client.delete_relation("document", prefixed_id, rd, target)
                deleted_relations.append({"relation": rd, "target": target})
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error("FGA cleanup failed for doc %s: %s", document_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Failed to clean up document permissions") from exc

    # DB delete — compensate FGA on failure by re-creating deleted relations
    try:
        await session.delete(document)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.error(
            "DB delete failed for doc %s after FGA cleanup — attempting compensation",
            document_id,
        )
        for dr in deleted_relations:
            try:
                await client.create_relation("document", prefixed_id, dr["relation"], dr["target"])
            except Exception:
                logger.warning(
                    "FGA compensation failed for doc %s relation %s->%s",
                    document_id,
                    dr["relation"],
                    dr["target"],
                )
        raise HTTPException(status_code=500, detail="Failed to delete document") from exc

    return {"status": "deleted", "id": document_id}


@router.post("/documents/{document_id}/share")
@limiter.limit(RATE_LIMIT_AUTH)
async def share_document(
    request: Request,
    document_id: DocumentId,
    body: ShareDocumentRequest,
    user_id: str = Depends(require_fga("document", "owner")),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
):
    """Share a document with another user. Only the owner can share."""
    document = await session.get(Document, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Prevent self-sharing (owner already has access)
    if body.user_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot share a document with yourself")

    # Verify target user exists and is in the same tenant
    try:
        client = get_descope_client()
        target_user = await client.load_user(body.user_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Target user not found") from exc
        logger.error(
            "Failed to verify target user %s: HTTP %s",
            body.user_id,
            exc.response.status_code,
        )
        raise HTTPException(status_code=502, detail="Failed to verify target user") from exc
    except httpx.RequestError as exc:
        logger.error(
            "Network error verifying target user %s: %s",
            body.user_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to verify target user") from exc

    if not target_user or not isinstance(target_user, dict):
        raise HTTPException(status_code=404, detail="Target user not found")

    target_tenants = [t.get("tenantId") for t in target_user.get("userTenants", []) if isinstance(t, dict)]
    if tenant_id not in target_tenants:
        raise HTTPException(
            status_code=403,
            detail="Cannot share with users outside your tenant",
        )

    # Create FGA relation with tenant-prefixed resource ID
    prefixed_id = _prefix_resource_id(tenant_id, document_id)
    try:
        await client.create_relation("document", prefixed_id, body.relation, body.user_id)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error(
            "Failed to create share relation for doc %s: %s",
            document_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to share document") from exc

    return {
        "document_id": document_id,
        "user_id": body.user_id,
        "relation": body.relation,
    }


@router.delete("/documents/{document_id}/share/{target_user_id}")
@limiter.limit(RATE_LIMIT_AUTH)
async def revoke_share(
    request: Request,
    document_id: DocumentId,
    target_user_id: str,
    user_id: str = Depends(require_fga("document", "owner")),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_async_session),
):
    """Revoke a user's access to a document. Deletes both viewer and editor relations."""
    document = await session.get(Document, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    try:
        client = get_descope_client()
    except Exception as exc:
        logger.error(
            "Failed to initialize client for revoke on doc %s: %s",
            document_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to revoke document access") from exc

    # Verify target user exists and is in the same tenant
    try:
        target_user = await client.load_user(target_user_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Target user not found") from exc
        logger.error(
            "Failed to verify target user %s: HTTP %s",
            target_user_id,
            exc.response.status_code,
        )
        raise HTTPException(status_code=502, detail="Failed to verify target user") from exc
    except httpx.RequestError as exc:
        logger.error(
            "Network error verifying target user %s: %s",
            target_user_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to verify target user") from exc

    if not target_user or not isinstance(target_user, dict):
        raise HTTPException(status_code=404, detail="Target user not found")

    target_tenants = [t.get("tenantId") for t in target_user.get("userTenants", []) if isinstance(t, dict)]
    if tenant_id not in target_tenants:
        raise HTTPException(
            status_code=403,
            detail="Cannot revoke access for users outside your tenant",
        )

    prefixed_id = _prefix_resource_id(tenant_id, document_id)
    for relation in ("viewer", "editor"):
        try:
            await client.delete_relation("document", prefixed_id, relation, target_user_id)
        except httpx.HTTPStatusError as exc:
            # Relation may not exist — tolerate 400/404 from Descope
            if exc.response.status_code not in (400, 404):
                logger.error(
                    "Failed to revoke %s for user %s on doc %s: HTTP %s",
                    relation,
                    target_user_id,
                    document_id,
                    exc.response.status_code,
                )
                raise HTTPException(
                    status_code=502,
                    detail="Failed to revoke document access",
                ) from exc
        except httpx.RequestError as exc:
            logger.error(
                "Network error revoking %s for user %s on doc %s: %s",
                relation,
                target_user_id,
                document_id,
                type(exc).__name__,
            )
            raise HTTPException(status_code=502, detail="Failed to revoke document access") from exc

    return {
        "status": "revoked",
        "document_id": document_id,
        "user_id": target_user_id,
    }
