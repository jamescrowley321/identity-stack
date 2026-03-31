import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    """Canonical user identity (SCIM-aligned field names)."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(index=True)
    user_name: str
    given_name: str | None = None
    family_name: str | None = None
    status: str = Field(default="active")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False, onupdate=func.now()),
    )


class IdPLink(SQLModel, table=True):
    """Links a canonical user to an external identity provider identity."""

    __tablename__ = "idp_links"
    __table_args__ = (UniqueConstraint("user_id", "provider_id", name="uq_idp_links_user_provider"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id")
    provider_id: uuid.UUID = Field(foreign_key="providers.id")
    external_sub: str = Field(index=True)
    external_email: str | None = None
    linked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    link_metadata: dict | None = Field(default=None, sa_column=Column("metadata", JSONB, nullable=True))
