"""KeycloakPullState — where the Keycloak event pull stands, one row per realm."""

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


class KeycloakPullState(SQLModel, table=True):
    """The pull's cursor in each event stream of a realm, and what it last knew of the realm.

    ``admin_cursor`` and ``user_cursor`` are Keycloak event times (milliseconds
    since the epoch): every event before them has been applied. ``admin_seen``
    and ``user_seen`` map the ids of the events applied at or shortly before the
    cursor to their times: a poll reads those again and does not apply them twice. ``organizations``
    maps a Keycloak organization id to its alias, and ``client_roles`` a client
    role id of the API client to ``{"name", "role_id", "permission_id"}``: an
    event that deletes one names only its Keycloak id.
    """

    __tablename__ = "keycloak_pull_state"

    # The realm's issuer URL (KEYCLOAK_ISSUER_URL).
    realm: str = Field(sa_column=sa.Column(sa.String, primary_key=True))
    admin_cursor: int = Field(default=0, sa_column=sa.Column(sa.BigInteger, nullable=False, server_default="0"))
    admin_seen: dict[str, int] = Field(
        default_factory=dict, sa_column=sa.Column(sa.JSON, nullable=False, server_default="{}")
    )
    user_cursor: int = Field(default=0, sa_column=sa.Column(sa.BigInteger, nullable=False, server_default="0"))
    user_seen: dict[str, int] = Field(
        default_factory=dict, sa_column=sa.Column(sa.JSON, nullable=False, server_default="{}")
    )
    organizations: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON, nullable=False, server_default="{}"))
    client_roles: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON, nullable=False, server_default="{}"))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=sa.Column(
            sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
        ),
    )
