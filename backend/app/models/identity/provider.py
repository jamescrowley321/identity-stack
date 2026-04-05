"""Canonical Provider model for registered IdP configurations."""

import enum
import uuid as uuid_mod
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class ProviderType(str, enum.Enum):
    descope = "descope"
    ory = "ory"
    entra = "entra"
    cognito = "cognito"
    oidc = "oidc"


class Provider(SQLModel, table=True):
    """Registered IdP configuration. No credentials stored — config_ref points to Infisical."""

    __tablename__ = "providers"

    id: uuid_mod.UUID = Field(default_factory=uuid_mod.uuid4, primary_key=True, sa_type=sa.Uuid)
    name: str = Field(sa_column=sa.Column(sa.String, nullable=False, unique=True))
    type: ProviderType = Field(
        sa_column=sa.Column(sa.Enum(ProviderType, name="provider_type"), nullable=False, index=True)
    )
    issuer_url: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    base_url: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    capabilities: list[str] = Field(
        default_factory=list, sa_column=sa.Column(sa.JSON, nullable=False, server_default="[]")
    )
    config_ref: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    active: bool = Field(default=True, sa_column=sa.Column(sa.Boolean, nullable=False, server_default=sa.text("true")))
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
