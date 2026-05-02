"""SyncEvent — append-only log of inbound identity sync activity."""

import enum
import uuid as uuid_mod
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class SyncEventVerb(str, enum.Enum):
    created = "created"
    updated = "updated"
    deleted = "deleted"
    linked = "linked"
    skipped = "skipped"
    failed = "failed"


class SyncEvent(SQLModel, table=True):
    """Inbound sync event. Append-only — no updates after insert."""

    __tablename__ = "sync_events"
    __table_args__ = (sa.Index("ix_sync_events_occurred_at", "occurred_at"),)

    id: uuid_mod.UUID = Field(default_factory=uuid_mod.uuid4, primary_key=True, sa_type=sa.Uuid)
    provider_id: uuid_mod.UUID | None = Field(
        default=None,
        sa_column=sa.Column(sa.Uuid, sa.ForeignKey("providers.id", ondelete="SET NULL"), nullable=True, index=True),
    )
    verb: SyncEventVerb = Field(
        sa_column=sa.Column(sa.Enum(SyncEventVerb, name="sync_event_verb"), nullable=False, index=True),
    )
    subject_type: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    subject_id: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    external_sub: str = Field(default="", sa_column=sa.Column(sa.String, nullable=False, server_default=""))
    detail: dict | None = Field(default=None, sa_column=sa.Column(sa.JSON, nullable=True))
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
