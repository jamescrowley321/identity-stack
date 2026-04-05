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
    """SCIM-aligned canonical user identity.

    Note: updated_at uses ORM-level onupdate=sa.func.now(), which only fires for
    ORM updates (session.commit()). Raw SQL UPDATE statements bypass this — if raw
    SQL updates are needed in the future, add a database-level trigger.
    """

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
    """Links a canonical user to an external identity provider identity.

    Note: The `metadata_` field uses a trailing underscore to avoid shadowing
    Python's built-in `metadata`. The actual DB column is named `metadata`.
    When writing queries, use the column name `metadata` (not `metadata_`).
    """

    __tablename__ = "idp_links"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "provider_id", name="uq_idp_links_user_provider"),
        sa.UniqueConstraint("provider_id", "external_sub", name="uq_idp_links_provider_external_sub"),
    )

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
