"""Seal the stored registration tokens of machine credentials (#698).

Until this revision ``machine_credentials.registration_token`` held Keycloak's
registration token in plain text, and Keycloak answers that token with the
client's secret. The upgrade seals every stored token with the first key of
``KEYCLOAK_REGISTRATION_TOKEN_KEYS`` (Fernet, as the backend does); the downgrade
opens them again. With no stored token neither needs the key; with one, an unset
or invalid key stops the migration rather than leave a token in plain text.

Both directions are safe to repeat. The upgrade leaves a value that already opens
under a configured key as it is: a backend of this revision may have sealed it
while the database was still at 010 (a new image started before ``alembic
upgrade``), or the revision was stamped back and run again. The downgrade leaves
a value that opens under no configured key as it is, rather than stop: it was
never sealed (a plain-text token) or was sealed under a key no longer listed,
and the 010 code answers such a token's 401 by deactivating the credential.

The key parsing is repeated here, not imported from ``app``, so this revision
keeps running whatever later happens to the application module.
"""

import os
from typing import Union

import sqlalchemy as sa
from alembic import context, op
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

# revision identifiers, used by Alembic.
revision: str = "011_seal_registration_tokens"
down_revision: Union[str, None] = "010_machine_credentials"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

KEYS_VARIABLE = "KEYCLOAK_REGISTRATION_TOKEN_KEYS"

_tokens = sa.table("machine_credentials", sa.column("id", sa.Uuid()), sa.column("registration_token", sa.String()))


_OFFLINE_GUARD = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM machine_credentials WHERE registration_token <> '') THEN "
    "RAISE EXCEPTION '011_seal_registration_tokens must run online (alembic without --sql) "
    "while machine_credentials holds registration tokens'; END IF; END $$"
)


def _fernet() -> MultiFernet:
    keys = [key.strip() for key in os.environ.get(KEYS_VARIABLE, "").split(",") if key.strip()]
    if not keys:
        raise RuntimeError(f"machine_credentials holds registration tokens: set {KEYS_VARIABLE} to migrate them")
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except ValueError as exc:
        raise RuntimeError(f"{KEYS_VARIABLE} holds a value that is not a Fernet key") from exc


def _opened(fernet: MultiFernet, value: str) -> str | None:
    """The value opened, or ``None`` when it opens under no configured key."""
    try:
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def _seal(fernet: MultiFernet, value: str) -> str | None:
    return None if _opened(fernet, value) is not None else fernet.encrypt(value.encode()).decode()


def _rewrite(transform) -> None:
    """Set each stored token to ``transform(fernet, token)``; a ``None`` leaves that row as it is.

    Offline (``--sql``) no row can be read and Fernet has no SQL form, so the
    script gets a guard instead: applied to a database holding a token, it stops
    before the revision is stamped. Only an online run seals or opens tokens.
    """
    if context.is_offline_mode():
        op.execute(_OFFLINE_GUARD)
        return
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(_tokens.c.id, _tokens.c.registration_token).where(_tokens.c.registration_token != "")
    ).all()
    if not rows:
        return
    fernet = _fernet()
    for row_id, token in rows:
        rewritten = transform(fernet, token)
        if rewritten is not None:
            connection.execute(sa.update(_tokens).where(_tokens.c.id == row_id).values(registration_token=rewritten))


def upgrade() -> None:
    _rewrite(_seal)


def downgrade() -> None:
    _rewrite(_opened)
