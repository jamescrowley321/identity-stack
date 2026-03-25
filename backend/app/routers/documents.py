from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from app.dependencies.fga import require_fga
from app.dependencies.tenant import get_tenant_id
from app.logging_config import get_logger
from app.models.database import get_session
from app.models.document import Document
from app.services.fga import DescopeFGAClient, get_fga_client

logger = get_logger(__name__)
router = APIRouter()


class CreateDocumentRequest(BaseModel):
    title: str
    content: str = ""


class UpdateDocumentRequest(BaseModel):
    title: str | None = None
    content: str | None = None


class ShareDocumentRequest(BaseModel):
    user_id: str
    relation: Literal["viewer", "editor"]


@router.post("/documents")
async def create_document(
    request: Request,
    body: CreateDocumentRequest,
    tenant_id: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """Create a document and auto-assign owner relation via FGA."""
    claims = getattr(request.state, "claims", None) or {}
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user identity")

    doc = Document(tenant_id=tenant_id, title=body.title, content=body.content, created_by=user_id)

    # Create FGA owner relation first — if this fails, we don't persist the document
    fga = get_fga_client()
    await fga.create_relation("document", doc.id, "owner", user_id)

    session.add(doc)
    session.commit()
    session.refresh(doc)

    logger.info("document.created id=%s tenant=%s", doc.id, tenant_id)
    return doc.model_dump()


@router.get("/documents")
async def list_documents(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """List documents in the current tenant that the user can view."""
    claims = getattr(request.state, "claims", None) or {}
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user identity")

    # Get document IDs the user can view from FGA
    fga = get_fga_client()
    viewable_ids = await fga.list_user_resources("document", "can_view", user_id)

    if not viewable_ids:
        return {"documents": []}

    # Filter to current tenant's documents
    docs = session.exec(select(Document).where(Document.tenant_id == tenant_id, Document.id.in_(viewable_ids))).all()
    return {"documents": [d.model_dump() for d in docs]}


@router.get("/documents/{document_id}")
async def get_document(
    document_id: str,
    _fga: DescopeFGAClient = Depends(require_fga("document", "can_view")),
    tenant_id: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """Get a document by ID. Requires can_view permission via FGA."""
    doc = session.get(Document, document_id)
    if not doc or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc.model_dump()


@router.put("/documents/{document_id}")
async def update_document(
    request: Request,
    document_id: str,
    body: UpdateDocumentRequest,
    _fga: DescopeFGAClient = Depends(require_fga("document", "can_edit")),
    tenant_id: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """Update a document. Requires can_edit permission via FGA."""
    doc = session.get(Document, document_id)
    if not doc or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if body.title is not None:
        doc.title = body.title
    if body.content is not None:
        doc.content = body.content
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc.model_dump()


@router.delete("/documents/{document_id}")
async def delete_document(
    request: Request,
    document_id: str,
    _fga: DescopeFGAClient = Depends(require_fga("document", "can_delete")),
    tenant_id: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """Delete a document. Requires can_delete (owner) permission via FGA."""
    doc = session.get(Document, document_id)
    if not doc or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Clean up FGA relations
    fga = get_fga_client()
    relations = await fga.list_relations("document", document_id)
    for rel in relations:
        await fga.delete_relation("document", document_id, rel.get("relation", ""), rel.get("target", ""))

    session.delete(doc)
    session.commit()
    return {"status": "deleted", "document_id": document_id}


@router.post("/documents/{document_id}/share")
async def share_document(
    request: Request,
    document_id: str,
    body: ShareDocumentRequest,
    _fga: DescopeFGAClient = Depends(require_fga("document", "can_delete")),  # only owner can share
    tenant_id: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """Share a document with another user. Only the owner can share."""
    doc = session.get(Document, document_id)
    if not doc or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    fga = get_fga_client()
    await fga.create_relation("document", document_id, body.relation, body.user_id)
    return {"status": "shared", "document_id": document_id, "user_id": body.user_id, "relation": body.relation}


@router.delete("/documents/{document_id}/share/{user_id}")
async def revoke_share(
    request: Request,
    document_id: str,
    user_id: str,
    _fga: DescopeFGAClient = Depends(require_fga("document", "can_delete")),  # only owner can revoke
    tenant_id: str = Depends(get_tenant_id),
    session: Session = Depends(get_session),
):
    """Revoke a user's access to a document. Only the owner can revoke."""
    doc = session.get(Document, document_id)
    if not doc or doc.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    fga = get_fga_client()
    # Remove all relations for this user on this document
    for relation in ["viewer", "editor"]:
        await fga.delete_relation("document", document_id, relation, user_id)
    return {"status": "revoked", "document_id": document_id, "user_id": user_id}
