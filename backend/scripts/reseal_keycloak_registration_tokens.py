"""Reseal Keycloak machine credential registration tokens under the current first key.

Follow-up to #698/#715 and issue #717: KEYCLOAK_REGISTRATION_TOKEN_KEYS lists several
Fernet keys. The first seals and every listed key opens, so a rotation to B,A keeps
A listed while any token is still sealed under it. This script lets an operator
re-seal the stored registration tokens of Keycloak machine credentials under the
current first key without downtime.

It is run as a module from backend/ so that app.* is importable:

    DATABASE_URL=postgresql+asyncpg://... \
    KEYCLOAK_REGISTRATION_TOKEN_KEYS="<comma-separated Fernet keys>" \
        python -m scripts.reseal_keycloak_registration_tokens

The script never logs a registration token or key: messages name only counts and
configuration variables. It exits non-zero on a missing or invalid
KEYCLOAK_REGISTRATION_TOKEN_KEYS or when tokens that open only under
non-primary keys or unreadable tokens remain after the final scan.

This tool does not verify that all running backend instances have been
reconfigured to use the new first key. Operators MUST perform a rolling rollout
and confirm that every writer uses the current first key before treating a run
with no remaining non-primary or unreadable tokens as safe for key removal.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.identity.machine_credential import MachineCredentialRow
from app.repositories.registration_tokens import RegistrationTokenCipher, RegistrationTokenUnreadable


@dataclass(frozen=True)
class ResealSummary:
    """Summary of a reseal run.

    - ``examined``: tokens seen in the first pass.
    - ``resealed``: tokens successfully moved to the current first key.
    - ``unreadable``: tokens that could not be opened under any configured key
      in the first pass; left untouched so delete keeps
      ``UNREADABLE_REGISTRATION_TOKEN`` semantics.
    - ``remaining_non_primary``: tokens that still open only under a non-primary
      key after the final scan.
    - ``remaining_unreadable``: tokens that still cannot be opened after the
      final scan.
    - ``cas_conflicts``: rows that changed between read and update and were
      still sealed under a non-primary key afterwards.
    """

    examined: int
    resealed: int
    unreadable: int
    remaining_non_primary: int
    remaining_unreadable: int
    cas_conflicts: int


async def reseal_keycloak_registration_tokens(
    session_factory: async_sessionmaker[AsyncSession], cipher: RegistrationTokenCipher
) -> ResealSummary:
    """Reseal stored Keycloak registration tokens under the cipher's first key.

    Examines every non-empty ``registration_token`` of a Keycloak machine
    credential. Tokens that already open under the first key are left as they
    are. Tokens that open only under a later key are re-sealed under the first
    key using a compare-and-set on the stored value. Tokens that open under no
    configured key are counted as unreadable and left untouched; deleting their
    credentials keeps the ``UNREADABLE_REGISTRATION_TOKEN`` behaviour from
    ``services.machine_credentials``.

    A second pass classifies the tokens after the reseal. Any token that still
    opens only under a non-primary key, or cannot be opened at all, means safe
    completion is not established and callers MUST treat the run as incomplete.
    """

    examined = 0
    resealed = 0
    unreadable = 0
    remaining_non_primary = 0
    remaining_unreadable = 0
    cas_conflicts = 0
    conflict_ids: set[object] = set()

    async with session_factory() as session:
        stmt = (
            sa.select(MachineCredentialRow.id, MachineCredentialRow.registration_token)
            .where(
                MachineCredentialRow.provider == "keycloak",
                MachineCredentialRow.registration_token != "",
            )
            .order_by(MachineCredentialRow.created_at, MachineCredentialRow.id)
        )
        rows = (await session.execute(stmt)).all()

        # First pass: move tokens that open under a non-primary key to the primary key.
        for credential_id, stored in rows:
            examined += 1
            if cipher.opens_with_primary(stored):
                continue
            try:
                token = cipher.open(stored)
            except RegistrationTokenUnreadable:
                unreadable += 1
                continue
            # Sealed under a later key: reseal under the first, using CAS on the stored value.
            resealed_value = cipher.primary_seal(token)
            result = await session.execute(
                sa.update(MachineCredentialRow)
                .where(
                    MachineCredentialRow.id == credential_id,
                    MachineCredentialRow.registration_token == stored,
                )
                .values(registration_token=resealed_value)
            )
            if result.rowcount and result.rowcount > 0:
                resealed += 1
            else:
                conflict_ids.add(credential_id)

        await session.commit()

        # Second pass: classify the tokens as primary, non-primary or unreadable.
        rows_after = (
            await session.execute(
                sa.select(MachineCredentialRow.id, MachineCredentialRow.registration_token).where(
                    MachineCredentialRow.provider == "keycloak",
                    MachineCredentialRow.registration_token != "",
                )
            )
        ).all()

        for credential_id, stored in rows_after:
            if cipher.opens_with_primary(stored):
                continue
            try:
                cipher.open(stored)
            except RegistrationTokenUnreadable:
                remaining_unreadable += 1
                continue
            # Still opens under a non-primary key.
            remaining_non_primary += 1
            if credential_id in conflict_ids:
                cas_conflicts += 1

    return ResealSummary(
        examined=examined,
        resealed=resealed,
        unreadable=unreadable,
        remaining_non_primary=remaining_non_primary,
        remaining_unreadable=remaining_unreadable,
        cas_conflicts=cas_conflicts,
    )


async def _run() -> int:  # pragma: no cover - exercised via subprocess from tests
    from app.models.database import get_session_factory
    from app.repositories.registration_tokens import RegistrationTokenKeysNotConfigured, registration_token_cipher

    try:
        cipher = registration_token_cipher()
    except RegistrationTokenKeysNotConfigured as exc:
        # The operator sees the configuration error and no tokens are touched.
        print(f"ERROR: {exc}")
        return 1

    session_factory = get_session_factory()
    summary = await reseal_keycloak_registration_tokens(session_factory, cipher)
    print(
        f"Resealed {summary.resealed} of {summary.examined} Keycloak registration tokens "
        f"(first-pass unreadable: {summary.unreadable}; "
        f"remaining_non_primary: {summary.remaining_non_primary}; "
        f"remaining_unreadable: {summary.remaining_unreadable}; "
        f"cas_conflicts: {summary.cas_conflicts}).",
    )
    if summary.remaining_non_primary == 0 and summary.remaining_unreadable == 0 and summary.cas_conflicts == 0:
        # The database now holds no tokens that open only under non-primary keys.
        # Safe removal of old keys still depends on every writer having been
        # configured to use the current primary key before this run; this script
        # cannot inspect running instances' configuration.
        print(
            "COMPLETED: no remaining non-primary or unreadable tokens. "
            "Old KEYCLOAK_REGISTRATION_TOKEN_KEYS entries may be removed only "
            "after operators have verified that all writers use the current "
            "first key."
        )
        return 0

    print(
        "INCOMPLETE: safe completion not established. Do not remove old keys. "
        "Investigate remaining non-primary or unreadable tokens and confirm "
        "all writers use the current first key before retrying."
    )
    return 2


def main() -> None:  # pragma: no cover - exercised via subprocess from tests
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":  # pragma: no cover
    main()
