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
from expression import Error, Ok
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import scripts.reseal_keycloak_registration_tokens as reseal_script_module
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
from app.services.machine_credentials import UNSEALED_REGISTRATION_TOKEN, CanonicalMachineCredentials
from scripts.reseal_keycloak_registration_tokens import ResealSummary, reseal_keycloak_registration_tokens

pytestmark = pytest.mark.asyncio

BACKEND = Path(__file__).resolve().parents[3]
TOKEN = "eyJhbGciOiJIUzUxMiJ9.registration-token.signature"
KEY = Fernet.generate_key().decode()
CIPHER = RegistrationTokenCipher([KEY])
BEFORE_SEALING = "010_machine_credentials"
# The revision under test. Later revisions move the schema, so the tests stop here, not at head.
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

    async def test_a_delete_of_an_unsealed_token_deactivates_and_calls_no_provider(self, db_session):
        """A row written before 011 is not "sealed under a lost key": deactivated, its value never read."""
        credential = _credential((await _tenant(db_session)).id)
        await MachineCredentialRepository(db_session, cipher=CIPHER).create(
            credential, role_ids=(), registration_token=TOKEN
        )
        await db_session.execute(
            sa.update(MachineCredentialRow)
            .where(MachineCredentialRow.id == credential.id)
            .values(registration_token=TOKEN)
        )
        push = AsyncMock(spec=MachineCredentialPush)
        service = CanonicalMachineCredentials(
            provider="keycloak", repository=MachineCredentialRepository(db_session, cipher=CIPHER), push=push
        )

        result = await service.delete(tenant_id=str(credential.tenant_id), credential_id=str(credential.id))

        assert result == Error(UNSEALED_REGISTRATION_TOKEN)
        stored = await MachineCredentialRepository(db_session).get(
            credential.tenant_id, credential.id, provider="keycloak"
        )
        assert stored.status is credentials.CredentialStatus.INACTIVE
        assert push.mock_calls == []

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


def _reseal_script(postgres_url: str, key: str | None) -> subprocess.CompletedProcess:
    """Run the reseal script as the operator would, against the test database.

    The environment names the database and optionally KEYCLOAK_REGISTRATION_TOKEN_KEYS.
    """
    env = {**os.environ, "DATABASE_URL": postgres_url}
    env.pop(KEYS_VARIABLE, None)
    if key is not None:
        env[KEYS_VARIABLE] = key
    return subprocess.run(
        [sys.executable, "-m", "scripts.reseal_keycloak_registration_tokens"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
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
        try:
            assert _alembic(postgres_url, "stamp", BEFORE_SEALING, key=None).returncode == 0
            yield credential_id
        finally:
            async with async_engine.begin() as conn:
                await conn.execute(sa.delete(Tenant).where(Tenant.id == tenant_id))
            restored = _alembic(postgres_url, "upgrade", SEALING, key=None)
            assert restored.returncode == 0, restored.stderr
            assert _alembic(postgres_url, "stamp", "head", key=None).returncode == 0

    async def _token(self, async_engine, credential_id: uuid.UUID) -> str:
        async with async_engine.connect() as conn:
            stmt = sa.select(MachineCredentialRow.registration_token).where(MachineCredentialRow.id == credential_id)
            return (await conn.execute(stmt)).scalar_one()

    async def test_the_upgrade_seals_a_stored_token_and_the_downgrade_opens_it(
        self, async_engine, postgres_url, plain_text_row
    ):
        sealed = _alembic(postgres_url, "upgrade", SEALING, key=KEY)
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
        refused = _alembic(postgres_url, "upgrade", SEALING, key=None)

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
        upgraded = _alembic(postgres_url, "upgrade", SEALING, key=KEY)

        assert upgraded.returncode == 0, upgraded.stderr
        assert await self._token(async_engine, plain_text_row) == self.SEALED

    @pytest.mark.parametrize("plain_text_row", [SEALED_UNDER_ANOTHER_KEY], indirect=True)
    async def test_the_upgrade_leaves_a_token_sealed_under_a_retired_key_as_it_is(
        self, async_engine, postgres_url, plain_text_row
    ):
        """Sealing it again would open to ciphertext, sent to Keycloak as the token: it stays unreadable instead."""
        upgraded = _alembic(postgres_url, "upgrade", SEALING, key=KEY)

        assert upgraded.returncode == 0, upgraded.stderr
        assert await self._token(async_engine, plain_text_row) == self.SEALED_UNDER_ANOTHER_KEY

    @pytest.mark.parametrize("plain_text_row", [SEALED_UNDER_ANOTHER_KEY], indirect=True)
    async def test_the_downgrade_leaves_a_token_no_key_opens_as_it_is(self, async_engine, postgres_url, plain_text_row):
        assert _alembic(postgres_url, "stamp", SEALING, key=None).returncode == 0

        downgraded = _alembic(postgres_url, "downgrade", BEFORE_SEALING, key=KEY)

        assert downgraded.returncode == 0, downgraded.stderr
        assert await self._token(async_engine, plain_text_row) == self.SEALED_UNDER_ANOTHER_KEY


class TestResealScript:
    """Resealing Keycloak registration tokens under the current first key (#717)."""

    NEW_KEY = Fernet.generate_key().decode()

    async def _insert_sealed_row(self, async_engine, sealed: str) -> tuple[uuid.UUID, uuid.UUID]:
        """Insert a machine credential row with the given stored registration_token.

        Returns the credential id and tenant id so the test can clean the row up.
        """
        credential_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        async with async_engine.begin() as conn:
            await conn.execute(sa.insert(Tenant).values(id=tenant_id, name=f"reseal-{tenant_id}"))
            row = MachineCredentialRow(
                id=credential_id,
                tenant_id=tenant_id,
                provider="keycloak",
                name="reseal",
                client_id=f"machine-{credential_id}",
                subject=f"sub-{credential_id}",
                external_id=f"kc-{credential_id}",
                registration_token=sealed,
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
            values = {column.name: getattr(row, column.name) for column in MachineCredentialRow.__table__.columns}
            await conn.execute(sa.insert(MachineCredentialRow).values(values))
        return credential_id, tenant_id

    async def _registration_token(self, async_engine, credential_id: uuid.UUID) -> str:
        async with async_engine.connect() as conn:
            stmt = sa.select(MachineCredentialRow.registration_token).where(MachineCredentialRow.id == credential_id)
            return (await conn.execute(stmt)).scalar_one()

    async def _remove_row(self, async_engine, credential_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        async with async_engine.begin() as conn:
            await conn.execute(sa.delete(MachineCredentialRow).where(MachineCredentialRow.id == credential_id))
            await conn.execute(sa.delete(Tenant).where(Tenant.id == tenant_id))

    def _session_factory(self, async_engine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False)

    async def test_a_token_sealed_under_A_is_resealed_for_drop_to_B_only(self, async_engine):
        """A -> B,A rotation -> reseal -> drop A: the token still opens under B."""
        sealed_under_a = RegistrationTokenCipher([KEY]).seal(TOKEN)
        credential_id, tenant_id = await self._insert_sealed_row(async_engine, sealed_under_a)
        try:
            rotated_cipher = RegistrationTokenCipher([self.NEW_KEY, KEY])
            summary = await reseal_keycloak_registration_tokens(self._session_factory(async_engine), rotated_cipher)

            stored = await self._registration_token(async_engine, credential_id)
            assert stored != sealed_under_a
            assert RegistrationTokenCipher([self.NEW_KEY]).open(stored) == TOKEN
            assert summary == ResealSummary(
                examined=1,
                resealed=1,
                unreadable=0,
                remaining_non_primary=0,
                remaining_unreadable=0,
                cas_conflicts=0,
            )
        finally:
            await self._remove_row(async_engine, credential_id, tenant_id)

    async def test_reseal_is_idempotent_when_run_twice(self, async_engine):
        sealed_under_a = RegistrationTokenCipher([KEY]).seal(TOKEN)
        credential_id, tenant_id = await self._insert_sealed_row(async_engine, sealed_under_a)
        try:
            rotated_cipher = RegistrationTokenCipher([self.NEW_KEY, KEY])
            factory = self._session_factory(async_engine)

            first = await reseal_keycloak_registration_tokens(factory, rotated_cipher)
            second = await reseal_keycloak_registration_tokens(factory, rotated_cipher)

            stored = await self._registration_token(async_engine, credential_id)
            assert RegistrationTokenCipher([self.NEW_KEY]).open(stored) == TOKEN
            assert first.examined == second.examined == 1
            assert (first.resealed, second.resealed) == (1, 0)
            assert (first.unreadable, second.unreadable) == (0, 0)
            assert (first.remaining_non_primary, second.remaining_non_primary) == (0, 0)
            assert (first.remaining_unreadable, second.remaining_unreadable) == (0, 0)
            assert (first.cas_conflicts, second.cas_conflicts) == (0, 0)
        finally:
            await self._remove_row(async_engine, credential_id, tenant_id)

    async def test_script_refuses_invalid_configuration_without_changing_tokens(self, async_engine, postgres_url):
        """The operator script fails on invalid KEYCLOAK_REGISTRATION_TOKEN_KEYS and touches no tokens."""
        sealed_under_a = RegistrationTokenCipher([KEY]).seal(TOKEN)
        credential_id, tenant_id = await self._insert_sealed_row(async_engine, sealed_under_a)
        try:
            invalid = "not-a-fernet-key-but-maybe-a-secret"

            result = _reseal_script(postgres_url, key=invalid)

            assert result.returncode != 0
            assert "ERROR:" in result.stdout
            assert KEYS_VARIABLE in result.stdout or KEYS_VARIABLE in result.stderr
            combined = result.stdout + result.stderr
            assert TOKEN not in combined
            assert invalid not in combined

            stored = await self._registration_token(async_engine, credential_id)
            assert stored == sealed_under_a
        finally:
            await self._remove_row(async_engine, credential_id, tenant_id)

    async def test_cas_conflict_is_reported_and_leaves_non_primary_token(self, async_engine, monkeypatch):
        """A concurrent writer changing the token mid-run is reported and leaves a non-primary token."""
        sealed_under_a = RegistrationTokenCipher([KEY]).seal(TOKEN)
        credential_id, tenant_id = await self._insert_sealed_row(async_engine, sealed_under_a)
        try:
            rotated_cipher = RegistrationTokenCipher([self.NEW_KEY, KEY])
            factory = self._session_factory(async_engine)

            original_execute = AsyncSession.execute

            async def _execute_with_conflict(self, statement, *args, **kwargs):  # type: ignore[override]
                from sqlalchemy.sql.dml import Update

                if isinstance(statement, Update):
                    # Simulate a concurrent writer that changes the stored token
                    # to a different ciphertext still sealed under KEY.
                    async with async_engine.begin() as conn:
                        concurrent_value = RegistrationTokenCipher([KEY]).seal(TOKEN + "-concurrent")
                        await conn.execute(
                            sa.update(MachineCredentialRow)
                            .where(MachineCredentialRow.id == credential_id)
                            .values(registration_token=concurrent_value)
                        )
                return await original_execute(self, statement, *args, **kwargs)

            monkeypatch.setattr(AsyncSession, "execute", _execute_with_conflict)

            summary = await reseal_keycloak_registration_tokens(factory, rotated_cipher)

            stored = await self._registration_token(async_engine, credential_id)
            # The token is still sealed under the old key; the reseal did not move it.
            assert RegistrationTokenCipher([self.NEW_KEY]).opens_with_primary(stored) is False
            assert RegistrationTokenCipher([KEY]).open(stored).startswith(TOKEN)
            assert summary.cas_conflicts == 1
            assert summary.remaining_non_primary == 1
        finally:
            await self._remove_row(async_engine, credential_id, tenant_id)

    async def test_concurrent_old_key_writer_inserts_new_token_and_run_is_incomplete(self, async_engine, monkeypatch):
        """A concurrent writer inserting a new old-key token during the run leaves remaining_non_primary tokens."""
        sealed_under_a = RegistrationTokenCipher([KEY]).seal(TOKEN)
        credential_id, tenant_id = await self._insert_sealed_row(async_engine, sealed_under_a)
        created_ids = {"credential": credential_id, "tenant": tenant_id, "concurrent": None, "concurrent_tenant": None}
        try:
            rotated_cipher = RegistrationTokenCipher([self.NEW_KEY, KEY])
            factory = self._session_factory(async_engine)

            original_execute = AsyncSession.execute
            select_calls = {"count": 0}

            async def _execute_with_concurrent_insert(self, statement, *args, **kwargs):  # type: ignore[override]
                from sqlalchemy.sql import Select

                if isinstance(statement, Select):
                    select_calls["count"] += 1
                    if select_calls["count"] == 2:
                        # Insert a new credential sealed under the old key after the reseal updates.
                        new_id = uuid.uuid4()
                        new_tenant = uuid.uuid4()
                        created_ids["concurrent"] = new_id
                        created_ids["concurrent_tenant"] = new_tenant
                        async with async_engine.begin() as conn:
                            await conn.execute(
                                sa.insert(Tenant).values(id=new_tenant, name=f"reseal-concurrent-{new_tenant}")
                            )
                            concurrent_token = RegistrationTokenCipher([KEY]).seal(TOKEN + "-writer")
                            row = MachineCredentialRow(
                                id=new_id,
                                tenant_id=new_tenant,
                                provider="keycloak",
                                name="reseal-concurrent",
                                client_id=f"machine-{new_id}",
                                subject=f"sub-{new_id}",
                                external_id=f"kc-{new_id}",
                                registration_token=concurrent_token,
                                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                            )
                            values = {
                                column.name: getattr(row, column.name)
                                for column in MachineCredentialRow.__table__.columns
                            }
                            await conn.execute(sa.insert(MachineCredentialRow).values(values))
                return await original_execute(self, statement, *args, **kwargs)

            monkeypatch.setattr(AsyncSession, "execute", _execute_with_concurrent_insert)

            summary = await reseal_keycloak_registration_tokens(factory, rotated_cipher)

            assert summary.remaining_non_primary >= 1
        finally:
            await self._remove_row(async_engine, credential_id, tenant_id)
            if created_ids["concurrent"] is not None and created_ids["concurrent_tenant"] is not None:
                await self._remove_row(async_engine, created_ids["concurrent"], created_ids["concurrent_tenant"])

    async def test_run_completed_exit_code_and_message_without_secrets(self, monkeypatch, capsys):
        """_run() exits 0 on a clean summary and prints COMPLETED without leaking secrets."""
        secret_token = "secret-token"
        secret_key = "secret-key"

        async def _fake_reseal(session_factory, cipher):  # type: ignore[override]
            return ResealSummary(
                examined=1,
                resealed=1,
                unreadable=0,
                remaining_non_primary=0,
                remaining_unreadable=0,
                cas_conflicts=0,
            )

        monkeypatch.setenv(KEYS_VARIABLE, secret_key)
        monkeypatch.setattr(
            "app.repositories.registration_tokens.registration_token_cipher",
            lambda: object(),
        )
        monkeypatch.setattr("app.models.database.get_session_factory", lambda: object())
        monkeypatch.setattr(
            "scripts.reseal_keycloak_registration_tokens.reseal_keycloak_registration_tokens",
            _fake_reseal,
        )

        exit_code = await reseal_script_module._run()

        assert exit_code == 0
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "COMPLETED:" in output
        assert "INCOMPLETE" not in output
        assert secret_token not in output
        assert secret_key not in output

    async def test_run_incomplete_on_remaining_non_primary(self, monkeypatch, capsys):
        """_run() exits 2 and prints INCOMPLETE when remaining_non_primary > 0."""
        secret_token = "secret-token"
        secret_key = "secret-key"

        async def _fake_reseal(session_factory, cipher):  # type: ignore[override]
            return ResealSummary(
                examined=2,
                resealed=1,
                unreadable=0,
                remaining_non_primary=1,
                remaining_unreadable=0,
                cas_conflicts=0,
            )

        monkeypatch.setenv(KEYS_VARIABLE, secret_key)
        monkeypatch.setattr(
            "app.repositories.registration_tokens.registration_token_cipher",
            lambda: object(),
        )
        monkeypatch.setattr("app.models.database.get_session_factory", lambda: object())
        monkeypatch.setattr(
            "scripts.reseal_keycloak_registration_tokens.reseal_keycloak_registration_tokens",
            _fake_reseal,
        )

        exit_code = await reseal_script_module._run()

        assert exit_code == 2
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "INCOMPLETE" in output
        assert "COMPLETED" not in output
        assert secret_token not in output
        assert secret_key not in output

    async def test_run_incomplete_on_remaining_unreadable(self, monkeypatch, capsys):
        """_run() exits 2 and prints INCOMPLETE when remaining_unreadable > 0."""
        secret_token = "secret-token"
        secret_key = "secret-key"

        async def _fake_reseal(session_factory, cipher):  # type: ignore[override]
            return ResealSummary(
                examined=3,
                resealed=1,
                unreadable=1,
                remaining_non_primary=0,
                remaining_unreadable=1,
                cas_conflicts=0,
            )

        monkeypatch.setenv(KEYS_VARIABLE, secret_key)
        monkeypatch.setattr(
            "app.repositories.registration_tokens.registration_token_cipher",
            lambda: object(),
        )
        monkeypatch.setattr("app.models.database.get_session_factory", lambda: object())
        monkeypatch.setattr(
            "scripts.reseal_keycloak_registration_tokens.reseal_keycloak_registration_tokens",
            _fake_reseal,
        )

        exit_code = await reseal_script_module._run()

        assert exit_code == 2
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "INCOMPLETE" in output
        assert "COMPLETED" not in output
        assert secret_token not in output
        assert secret_key not in output

    async def test_run_incomplete_on_cas_conflicts(self, monkeypatch, capsys):
        """_run() exits 2 and prints INCOMPLETE when cas_conflicts > 0."""
        secret_token = "secret-token"
        secret_key = "secret-key"

        async def _fake_reseal(session_factory, cipher):  # type: ignore[override]
            return ResealSummary(
                examined=4,
                resealed=2,
                unreadable=0,
                remaining_non_primary=0,
                remaining_unreadable=0,
                cas_conflicts=1,
            )

        monkeypatch.setenv(KEYS_VARIABLE, secret_key)
        monkeypatch.setattr(
            "app.repositories.registration_tokens.registration_token_cipher",
            lambda: object(),
        )
        monkeypatch.setattr("app.models.database.get_session_factory", lambda: object())
        monkeypatch.setattr(
            "scripts.reseal_keycloak_registration_tokens.reseal_keycloak_registration_tokens",
            _fake_reseal,
        )

        exit_code = await reseal_script_module._run()

        assert exit_code == 2
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "INCOMPLETE" in output
        assert "COMPLETED" not in output
        assert secret_token not in output
        assert secret_key not in output
