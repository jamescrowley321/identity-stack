"""Registration tokens of machine credentials are sealed in Postgres, never stored in plain text (#698)."""

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from cryptography.fernet import Fernet
from expression import Ok

from app.domain import credentials
from app.models.identity.machine_credential import MachineCredentialRow
from app.models.identity.tenant import Tenant
from app.repositories.machine_credential import MachineCredentialRepository
from app.repositories.registration_tokens import (
    KEYS_VARIABLE,
    RegistrationTokenCipher,
    RegistrationTokenKeysNotConfigured,
    RegistrationTokenUnreadable,
)
from app.services.adapters.base import MachineCredentialPush
from app.services.machine_credentials import CanonicalMachineCredentials

pytestmark = pytest.mark.asyncio

BACKEND = Path(__file__).resolve().parents[3]
TOKEN = "eyJhbGciOiJIUzUxMiJ9.registration-token.signature"
KEY = Fernet.generate_key().decode()
CIPHER = RegistrationTokenCipher([KEY])
BEFORE_SEALING = "010_machine_credentials"
SEALING = "011_seal_registration_tokens"


def _credential(tenant_id: uuid.UUID) -> credentials.MachineCredential:
    suffix = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    return credentials.MachineCredential(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="keycloak",
        name=f"ci-{suffix}",
        client_id=f"machine-{suffix}",
        subject=f"sub-{suffix}",
        external_id=f"kc-{suffix}",
        status=credentials.CredentialStatus.ACTIVE,
        expires_at=now + timedelta(days=1),
        created_at=now,
        role_names=(),
    )


async def _tenant(session) -> Tenant:
    tenant = Tenant(name=f"sealed-{uuid.uuid4()}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _stored(session, credential_id: uuid.UUID) -> str:
    stmt = sa.select(MachineCredentialRow.registration_token).where(MachineCredentialRow.id == credential_id)
    return (await session.execute(stmt)).scalar_one()


class TestRepository:
    async def test_the_column_holds_ciphertext_that_opens_to_the_token(self, db_session):
        credential = _credential((await _tenant(db_session)).id)
        repository = MachineCredentialRepository(db_session, cipher=CIPHER)

        await repository.create(credential, role_ids=(), registration_token=TOKEN)

        stored = await _stored(db_session, credential.id)
        assert TOKEN not in stored
        assert (CIPHER.open(stored), await repository.registration_token(credential.id)) == (TOKEN, TOKEN)

    async def test_a_token_does_not_open_under_another_key(self, db_session):
        credential = _credential((await _tenant(db_session)).id)
        await MachineCredentialRepository(db_session, cipher=CIPHER).create(
            credential, role_ids=(), registration_token=TOKEN
        )
        other = RegistrationTokenCipher([Fernet.generate_key().decode()])

        with pytest.raises(RegistrationTokenUnreadable):
            await MachineCredentialRepository(db_session, cipher=other).registration_token(credential.id)

    async def test_delete_under_another_key_deactivates_and_best_effort_disables_service_account(self, db_session):
        """#698: sealed under key A, read under key B: the credential's tokens are refused, not a 500."""
        credential = _credential((await _tenant(db_session)).id)
        await MachineCredentialRepository(db_session, cipher=CIPHER).create(
            credential, role_ids=(), registration_token=TOKEN
        )
        push = AsyncMock(spec=MachineCredentialPush)
        push.set_machine_client_enabled.return_value = Ok(None)
        push.machine_client_exists.return_value = Ok(False)
        other = RegistrationTokenCipher([Fernet.generate_key().decode()])
        service = CanonicalMachineCredentials(
            provider="keycloak", repository=MachineCredentialRepository(db_session, cipher=other), push=push
        )

        result = await service.delete(tenant_id=str(credential.tenant_id), credential_id=str(credential.id))

        assert result == Ok(None)
        stored = await MachineCredentialRepository(db_session).get(
            credential.tenant_id, credential.id, provider="keycloak"
        )
        assert stored is None
        push.set_machine_client_enabled.assert_awaited_once()
        push.delete_machine_client.assert_not_awaited()
        push.machine_client_exists.assert_awaited_once()

    async def test_without_a_cipher_no_token_is_written_or_read(self, db_session):
        credential = _credential((await _tenant(db_session)).id)
        repository = MachineCredentialRepository(db_session)

        with pytest.raises(RegistrationTokenKeysNotConfigured):
            await repository.create(credential, role_ids=(), registration_token=TOKEN)
        with pytest.raises(RegistrationTokenKeysNotConfigured):
            await repository.registration_token(credential.id)


def _alembic(postgres_url: str, *args: str, key: str | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": postgres_url}
    env.pop(KEYS_VARIABLE, None)
    if key is not None:
        env[KEYS_VARIABLE] = key
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60
    )


class TestMigration:
    """``011_seal_registration_tokens`` seals the tokens 010 stored in plain text, and the downgrade opens them.

    The revision changes data only, so moving the shared test database across it
    leaves every other test's schema as it was. The row is committed and removed.
    """

    @pytest_asyncio.fixture(loop_scope="session")
    async def plain_text_row(self, request, async_engine, postgres_url):
        """A row of 010 holding ``TOKEN``, or the stored value an indirect parameter gives."""
        stored_value = getattr(request, "param", TOKEN)
        credential_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        async with async_engine.begin() as conn:
            await conn.execute(sa.insert(Tenant).values(id=tenant_id, name=f"sealed-{tenant_id}"))
            row = MachineCredentialRow(
                id=credential_id,
                tenant_id=tenant_id,
                provider="keycloak",
                name="plain",
                client_id=f"machine-{credential_id}",
                subject=f"sub-{credential_id}",
                external_id=f"kc-{credential_id}",
                registration_token=stored_value,
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
            values = {column.name: getattr(row, column.name) for column in MachineCredentialRow.__table__.columns}
            await conn.execute(sa.insert(MachineCredentialRow).values(values))
        assert _alembic(postgres_url, "stamp", BEFORE_SEALING, key=None).returncode == 0
        try:
            yield credential_id
        finally:
            async with async_engine.begin() as conn:
                await conn.execute(sa.delete(Tenant).where(Tenant.id == tenant_id))
            restored = _alembic(postgres_url, "upgrade", "head", key=None)
            assert restored.returncode == 0, restored.stderr

    async def _token(self, async_engine, credential_id: uuid.UUID) -> str:
        async with async_engine.connect() as conn:
            stmt = sa.select(MachineCredentialRow.registration_token).where(MachineCredentialRow.id == credential_id)
            return (await conn.execute(stmt)).scalar_one()

    async def test_the_upgrade_seals_a_stored_token_and_the_downgrade_opens_it(
        self, async_engine, postgres_url, plain_text_row
    ):
        sealed = _alembic(postgres_url, "upgrade", "head", key=KEY)
        assert sealed.returncode == 0, sealed.stderr
        stored = await self._token(async_engine, plain_text_row)
        assert TOKEN not in stored
        assert CIPHER.open(stored) == TOKEN

        opened = _alembic(postgres_url, "downgrade", BEFORE_SEALING, key=KEY)
        assert opened.returncode == 0, opened.stderr
        assert await self._token(async_engine, plain_text_row) == TOKEN

    async def test_the_upgrade_refuses_to_leave_a_token_in_plain_text_without_a_key(
        self, async_engine, postgres_url, plain_text_row
    ):
        refused = _alembic(postgres_url, "upgrade", "head", key=None)

        assert refused.returncode != 0
        assert KEYS_VARIABLE in refused.stderr
        assert await self._token(async_engine, plain_text_row) == TOKEN

    @pytest.mark.parametrize(
        "command", [("upgrade", f"{BEFORE_SEALING}:{SEALING}"), ("downgrade", f"{SEALING}:{BEFORE_SEALING}")]
    )
    async def test_offline_sql_stops_on_a_database_holding_tokens(
        self, async_engine, postgres_url, plain_text_row, command
    ):
        """``--sql`` cannot seal: its script refuses to run, and so to stamp 011, while a token is stored."""
        generated = _alembic(postgres_url, *command, "--sql", key=None)
        assert generated.returncode == 0, generated.stderr
        guard = next(line for line in generated.stdout.splitlines() if line.startswith("DO $$"))

        async with async_engine.connect() as conn:
            with pytest.raises(sa.exc.DBAPIError, match="must run online"):
                await conn.execute(sa.text(guard))
        assert await self._token(async_engine, plain_text_row) == TOKEN

    SEALED = CIPHER.seal(TOKEN)
    SEALED_UNDER_ANOTHER_KEY = RegistrationTokenCipher([Fernet.generate_key().decode()]).seal(TOKEN)

    @pytest.mark.parametrize("plain_text_row", [SEALED], indirect=True)
    async def test_the_upgrade_leaves_an_already_sealed_token_as_it_is(
        self, async_engine, postgres_url, plain_text_row
    ):
        """A backend of this revision sealed it while the database was still at 010: it is not sealed twice."""
        upgraded = _alembic(postgres_url, "upgrade", "head", key=KEY)

        assert upgraded.returncode == 0, upgraded.stderr
        assert await self._token(async_engine, plain_text_row) == self.SEALED

    @pytest.mark.parametrize("plain_text_row", [SEALED_UNDER_ANOTHER_KEY], indirect=True)
    async def test_the_downgrade_leaves_a_token_no_key_opens_as_it_is(self, async_engine, postgres_url, plain_text_row):
        assert _alembic(postgres_url, "stamp", "head", key=None).returncode == 0

        downgraded = _alembic(postgres_url, "downgrade", BEFORE_SEALING, key=KEY)

        assert downgraded.returncode == 0, downgraded.stderr
        assert await self._token(async_engine, plain_text_row) == self.SEALED_UNDER_ANOTHER_KEY
