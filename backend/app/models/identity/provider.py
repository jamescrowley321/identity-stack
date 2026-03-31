import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, SQLModel


class Provider(SQLModel, table=True):
    """Registered identity provider configuration."""

    __tablename__ = "providers"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    type: str  # descope, ory, entra, cognito, oidc
    issuer_url: str | None = None
    base_url: str | None = None
    capabilities: list[str] = Field(
        default_factory=list, sa_column=Column(ARRAY(String), nullable=False, server_default="{}")
    )
    config_ref: str | None = None
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
