"""Canonical Role, Permission, and RolePermission models."""

import uuid as uuid_mod
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class Role(SQLModel, table=True):
    """Role definition. tenant_id=NULL means global role."""

    __tablename__ = "roles"
    __table_args__ = (sa.UniqueConstraint("name", "tenant_id", name="uq_roles_name_tenant"),)

    id: uuid_mod.UUID = Field(default_factory=uuid_mod.uuid4, primary_key=True, sa_type=sa.Uuid)
    name: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    description: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    tenant_id: uuid_mod.UUID | None = Field(
        default=None,
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True),
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


class Permission(SQLModel, table=True):
    """Permission definition (e.g. 'documents.write')."""

    __tablename__ = "permissions"

    id: uuid_mod.UUID = Field(default_factory=uuid_mod.uuid4, primary_key=True, sa_type=sa.Uuid)
    name: str = Field(sa_column=sa.Column(sa.String, nullable=False, unique=True, index=True))
    description: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
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


class RolePermission(SQLModel, table=True):
    """Many-to-many mapping between roles and permissions."""

    __tablename__ = "role_permissions"

    role_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, primary_key=True)
    )
    permission_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(
            sa.Uuid, sa.ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False, primary_key=True
        )
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
