"""Canonical machine credentials: what a machine signs in with, owned by one tenant.

A row stands for one credential the sync provider holds (a Keycloak client with a
service account): ``client_id`` is what the machine authenticates as, ``subject``
the ``sub`` of its tokens and ``external_id`` the provider's own id. The roles it
holds in its tenant are its ``machine_credential_roles``. The provider's secret is
never stored; ``registration_token`` is the provider's token for managing (deleting)
that one client, sealed under ``KEYCLOAK_REGISTRATION_TOKEN_KEYS``
(``app.repositories.registration_tokens``, #698), and is never returned by the API
or logged.
"""

import enum
import uuid as uuid_mod
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class MachineCredentialStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"


class MachineCredentialRow(SQLModel, table=True):
    __tablename__ = "machine_credentials"
    __table_args__ = (sa.UniqueConstraint("provider", "subject", name="uq_machine_credentials_provider_subject"),)

    id: uuid_mod.UUID = Field(default_factory=uuid_mod.uuid4, primary_key=True, sa_type=sa.Uuid)
    tenant_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    provider: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    name: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    client_id: str = Field(sa_column=sa.Column(sa.String, nullable=False, unique=True))
    subject: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    external_id: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    registration_token: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    status: MachineCredentialStatus = Field(
        default=MachineCredentialStatus.active,
        sa_column=sa.Column(
            sa.Enum(MachineCredentialStatus, name="machine_credential_status"),
            nullable=False,
            server_default="active",
        ),
    )
    expires_at: datetime = Field(sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class MachineCredentialRole(SQLModel, table=True):
    """A role a machine credential holds in its tenant."""

    __tablename__ = "machine_credential_roles"

    credential_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(
            sa.Uuid, sa.ForeignKey("machine_credentials.id", ondelete="CASCADE"), primary_key=True, nullable=False
        )
    )
    role_id: uuid_mod.UUID = Field(
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    )
