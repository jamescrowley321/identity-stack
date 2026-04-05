"""Canonical Tenant model (distinct from existing TenantResource)."""

import enum
import uuid as uuid_mod
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class TenantStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"


class Tenant(SQLModel, table=True):
    """Organization/workspace in the canonical identity model.

    Note: updated_at uses ORM-level onupdate=sa.func.now(), which only fires for
    ORM updates (session.commit()). Raw SQL UPDATE statements bypass this — if raw
    SQL updates are needed in the future, add a database-level trigger.
    """

    __tablename__ = "tenants"

    id: uuid_mod.UUID = Field(default_factory=uuid_mod.uuid4, primary_key=True, sa_type=sa.Uuid)
    name: str = Field(sa_column=sa.Column(sa.String, nullable=False, unique=True))
    domains: list[str] = Field(default_factory=list, sa_column=sa.Column(sa.JSON, nullable=False, server_default="[]"))
    status: TenantStatus = Field(
        default=TenantStatus.active,
        sa_column=sa.Column(sa.Enum(TenantStatus, name="tenant_status"), nullable=False, server_default="active"),
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
