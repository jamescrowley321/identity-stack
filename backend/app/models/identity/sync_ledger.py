"""PendingPush and ReconciliationRun — what reconciliation knows of the push provider.

``pending_pushes`` rows are written by database triggers (migration 009), never by
the application: a row is a canonical entity changed since a reconciliation pass
last confirmed it in the provider the canonical model is pushed to.
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class PendingPush(SQLModel, table=True):
    """A canonical entity whose current state the push provider has not been confirmed to hold.

    ``kind`` is ``tenant``, ``user``, ``permission``, ``role`` or ``membership``;
    ``key`` the entity's id, or ``<user id>:<tenant id>`` for a membership (every
    role of a user in a tenant). ``seq`` grows with every change of the entity.
    ``snapshot`` keeps what a delete push needs once the row is gone (a
    permission's name, a role's name and tenant).
    """

    __tablename__ = "pending_pushes"

    kind: str = Field(sa_column=sa.Column(sa.String, primary_key=True))
    key: str = Field(sa_column=sa.Column(sa.String, primary_key=True))
    seq: int = Field(sa_column=sa.Column(sa.BigInteger, nullable=False))
    snapshot: dict | None = Field(default=None, sa_column=sa.Column(sa.JSON, nullable=True))
    marked_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


class ReconciliationRun(SQLModel, table=True):
    """The outcome of a provider's last reconciliation pass."""

    __tablename__ = "reconciliation_runs"

    provider: str = Field(sa_column=sa.Column(sa.String, primary_key=True))
    finished_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    status: str = Field(sa_column=sa.Column(sa.String, nullable=False))
    stats: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON, nullable=False, server_default="{}"))
    error: str | None = Field(default=None, sa_column=sa.Column(sa.String, nullable=True))
