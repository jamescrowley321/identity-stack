import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

from app.models.types import UTCDateTime


class Document(SQLModel, table=True):
    """A document scoped to a specific tenant with FGA-enforced access control."""

    __tablename__ = "documents"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    tenant_id: str = Field(index=True)
    title: str
    content: str = ""
    created_by: str
    # Explicit column so the type does not depend on the installed sqlmodel's
    # default mapping for `datetime`; matches migration 001 (timestamptz NOT NULL).
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(UTCDateTime(), nullable=False, server_default=sa.func.now()),
    )
