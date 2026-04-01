"""Canonical User and IdPLink models (SCIM-aligned)."""

import enum
import uuid as uuid_mod
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class UserStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    provisioned = "provisioned"


class User(SQLModel, table=True):
    """SCIM-aligned canonical user identity."""

    __tablename__ = "users"

    id: uuid_mod.UUID = Field(default_factory=uuid_mod.uuid4, primary_key=True, sa_type=sa.Uuid)
    email: str = Field(sa_column=sa.Column(sa.String, nullable=False, unique=True, index=True))
    user_name: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    given_name: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    family_name: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    status: UserStatus = Field(
        default=UserStatus.active,
        sa_column=sa.Column(sa.Enum(UserStatus, name="userstatus"), nullable=False, server_default="active"),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(
            sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
        ),
    )


class IdPLink(SQLModel, table=True):
    """Links a canonical user to an external identity provider identity."""

    __tablename__ = "idp_links"
    __table_args__ = (sa.UniqueConstraint("user_id", "provider_id", name="uq_idp_links_user_provider"),)

    id: uuid_mod.UUID = Field(default_factory=uuid_mod.uuid4, primary_key=True, sa_type=sa.Uuid)
    user_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    provider_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("providers.id", ondelete="CASCADE"), nullable=False)
    )
    external_sub: str = Field(sa_column=sa.Column(sa.String, nullable=False, index=True))
    external_email: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    linked_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    metadata_: dict | None = Field(
        default=None,
        sa_column=sa.Column("metadata", sa.JSON, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(
            sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
        ),
    )
