"""Seal the stored registration tokens of machine credentials (#698).

Until this revision ``machine_credentials.registration_token`` held Keycloak's
registration token in plain text, and Keycloak answers that token with the
client's secret. The upgrade seals every stored token with the first key of
``KEYCLOAK_REGISTRATION_TOKEN_KEYS`` (Fernet, as the backend does); the downgrade
opens them again. With no stored token neither needs the key; with one, an unset
or invalid key stops the migration rather than leave a token in plain text.

The key parsing is repeated here, not imported from ``app``, so this revision
keeps running whatever later happens to the application module.
"""

import os
from typing import Union

import sqlalchemy as sa
from alembic import op
from cryptography.fernet import Fernet, MultiFernet

# revision identifiers, used by Alembic.
revision: str = "011_seal_registration_tokens"
down_revision: Union[str, None] = "010_machine_credentials"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

KEYS_VARIABLE = "KEYCLOAK_REGISTRATION_TOKEN_KEYS"

_tokens = sa.table("machine_credentials", sa.column("id", sa.Uuid()), sa.column("registration_token", sa.String()))


def _fernet() -> MultiFernet:
    keys = [key.strip() for key in os.environ.get(KEYS_VARIABLE, "").split(",") if key.strip()]
    if not keys:
        raise RuntimeError(f"machine_credentials holds registration tokens: set {KEYS_VARIABLE} to migrate them")
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except ValueError as exc:
        raise RuntimeError(f"{KEYS_VARIABLE} holds a value that is not a Fernet key") from exc


def _rewrite(transform) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(_tokens.c.id, _tokens.c.registration_token).where(_tokens.c.registration_token != "")
    ).all()
    if not rows:
        return
    fernet = _fernet()
    for row_id, token in rows:
        connection.execute(
            sa.update(_tokens)
            .where(_tokens.c.id == row_id)
            .values(registration_token=transform(fernet, token.encode()).decode())
        )


def upgrade() -> None:
    _rewrite(lambda fernet, token: fernet.encrypt(token))


def downgrade() -> None:
    _rewrite(lambda fernet, token: fernet.decrypt(token))
