import uuid
from datetime import datetime, timezone

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class UserTenantRole(SQLModel, table=True):
    """Per-tenant role assignment with audit trail."""

    __tablename__ = "user_tenant_roles"
    __table_args__ = (Index("ix_user_tenant_roles_user_tenant", "user_id", "tenant_id"),)

    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True)
    tenant_id: uuid.UUID = Field(foreign_key="tenants.id", primary_key=True)
    role_id: uuid.UUID = Field(foreign_key="roles.id", primary_key=True)
    assigned_by: uuid.UUID | None = Field(default=None, foreign_key="users.id")
    assigned_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
