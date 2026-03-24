import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class TenantResource(SQLModel, table=True):
    """A demo resource scoped to a specific tenant. Demonstrates data isolation."""

    __tablename__ = "tenant_resources"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
