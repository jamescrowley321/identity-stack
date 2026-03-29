import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class Document(SQLModel, table=True):
    """A document scoped to a specific tenant with FGA-enforced access control."""

    __tablename__ = "documents"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    tenant_id: str = Field(index=True)
    title: str
    content: str = ""
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
