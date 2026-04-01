"""Canonical UserTenantRole assignment model."""

import uuid as uuid_mod
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class UserTenantRole(SQLModel, table=True):
    """Per-tenant role assignment with audit trail."""

    __tablename__ = "user_tenant_roles"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "tenant_id", "role_id", name="uq_user_tenant_roles_user_tenant_role"),
        sa.Index("ix_user_tenant_roles_user_tenant", "user_id", "tenant_id"),
    )

    user_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, primary_key=True)
    )
    tenant_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, primary_key=True)
    )
    role_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, primary_key=True)
    )
    assigned_by: uuid_mod.UUID | None = Field(
        default=None,
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    assigned_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
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
