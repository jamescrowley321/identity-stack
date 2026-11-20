"""Queries over the canonical machine credentials and the roles they hold."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import credentials
from app.models.identity.machine_credential import MachineCredentialRole, MachineCredentialRow, MachineCredentialStatus
from app.models.identity.role import Permission, Role, RolePermission
from app.repositories.base import RepositoryConflictError
from app.repositories.registration_tokens import RegistrationTokenCipher, RegistrationTokenKeysNotConfigured


def credential_from_row(row: MachineCredentialRow, role_names: tuple[str, ...]) -> credentials.MachineCredential:
    return credentials.MachineCredential(
        id=row.id,
        tenant_id=row.tenant_id,
        provider=row.provider,
        name=row.name,
        client_id=row.client_id,
        subject=row.subject,
        external_id=row.external_id,
        status=credentials.CredentialStatus(row.status.value),
        expires_at=row.expires_at,
        created_at=row.created_at,
        role_names=role_names,
    )


def credential_to_row(
    credential: credentials.MachineCredential, *, sealed_registration_token: str
) -> MachineCredentialRow:
    return MachineCredentialRow(
        id=credential.id,
        tenant_id=credential.tenant_id,
        provider=credential.provider,
        name=credential.name,
        client_id=credential.client_id,
        subject=credential.subject,
        external_id=credential.external_id,
        registration_token=sealed_registration_token,
        status=MachineCredentialStatus(credential.status.value),
        expires_at=credential.expires_at,
        created_at=credential.created_at,
    )


class MachineCredentialRepository:
    """The ``machine_credentials`` rows of one provider and their roles.

    Registration tokens are sealed with ``cipher`` before they are written and
    opened when read (#698); a repository built without one cannot write or read
    a token, so none is ever stored in plain text.
    """

    def __init__(self, session: AsyncSession, *, cipher: RegistrationTokenCipher | None = None) -> None:
        self._session = session
        self._cipher = cipher

    def _token_cipher(self) -> RegistrationTokenCipher:
        if self._cipher is None:
            raise RegistrationTokenKeysNotConfigured("this repository was built without a registration token cipher")
        return self._cipher

    async def roles_named(self, tenant_id: uuid.UUID, names: tuple[str, ...]) -> dict[str, uuid.UUID]:
        """The id of each named role a credential of the tenant may hold: the tenant's own, else a global one."""
        if not names:
            return {}
        stmt = sa.select(Role.name, Role.id, Role.tenant_id).where(
            Role.name.in_(names), sa.or_(Role.tenant_id == tenant_id, Role.tenant_id.is_(None))
        )
        found: dict[str, uuid.UUID] = {}
        for name, role_id, role_tenant in (await self._session.execute(stmt)).all():
            if role_tenant is not None or name not in found:
                found[name] = role_id
        return found

    async def _role_names(self, credential_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str, ...]]:
        if not credential_ids:
            return {}
        stmt = (
            sa.select(MachineCredentialRole.credential_id, Role.name)
            .join(Role, Role.id == MachineCredentialRole.role_id)
            .where(MachineCredentialRole.credential_id.in_(credential_ids))
            .order_by(Role.name)
        )
        names: dict[uuid.UUID, list[str]] = {}
        for credential_id, name in (await self._session.execute(stmt)).all():
            names.setdefault(credential_id, []).append(name)
        return {credential_id: tuple(found) for credential_id, found in names.items()}

    async def _from_rows(self, rows: list[MachineCredentialRow]) -> list[credentials.MachineCredential]:
        names = await self._role_names([row.id for row in rows])
        return [credential_from_row(row, names.get(row.id, ())) for row in rows]

    async def create(
        self,
        credential: credentials.MachineCredential,
        *,
        role_ids: tuple[uuid.UUID, ...],
        registration_token: str,
    ) -> None:
        """Add the credential and its roles. Raises RepositoryConflictError on a taken client id or subject."""
        sealed = self._token_cipher().seal(registration_token)
        self._session.add(credential_to_row(credential, sealed_registration_token=sealed))
        try:
            await self._session.flush()
            for role_id in role_ids:
                self._session.add(MachineCredentialRole(credential_id=credential.id, role_id=role_id))
            await self._session.flush()
        except IntegrityError as exc:
            raise RepositoryConflictError(str(exc)) from exc

    async def get(
        self, tenant_id: uuid.UUID, credential_id: uuid.UUID, *, provider: str
    ) -> credentials.MachineCredential | None:
        """The provider's credential of that id in the tenant, or ``None``."""
        row = await self._session.get(MachineCredentialRow, credential_id)
        if row is None or row.tenant_id != tenant_id or row.provider != provider:
            return None
        return (await self._from_rows([row]))[0]

    async def list_for_tenant(self, tenant_id: uuid.UUID, *, provider: str) -> list[credentials.MachineCredential]:
        """The provider's credentials of the tenant, newest first."""
        stmt = (
            sa.select(MachineCredentialRow)
            .where(MachineCredentialRow.tenant_id == tenant_id, MachineCredentialRow.provider == provider)
            .order_by(MachineCredentialRow.created_at.desc(), MachineCredentialRow.id)
        )
        return await self._from_rows(list((await self._session.execute(stmt)).scalars().all()))

    async def by_subject(self, provider: str, subject: str) -> credentials.MachineCredential | None:
        """The provider's credential whose tokens carry ``subject``, or ``None``."""
        stmt = sa.select(MachineCredentialRow).where(
            MachineCredentialRow.provider == provider, MachineCredentialRow.subject == subject
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return None if row is None else (await self._from_rows([row]))[0]

    async def permissions(self, credential_id: uuid.UUID) -> dict[str, tuple[str, ...]]:
        """Each role the credential holds, by name, with that role's permissions."""
        stmt = (
            sa.select(Role.name, Permission.name)
            .select_from(MachineCredentialRole)
            .join(Role, Role.id == MachineCredentialRole.role_id)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(Permission, Permission.id == RolePermission.permission_id)
            .where(MachineCredentialRole.credential_id == credential_id)
            .order_by(Role.name, Permission.name)
        )
        grants: dict[str, list[str]] = {}
        for role_name, permission_name in (await self._session.execute(stmt)).all():
            held = grants.setdefault(role_name, [])
            if permission_name is not None:
                held.append(permission_name)
        return {role_name: tuple(held) for role_name, held in grants.items()}

    async def set_status(self, credential_id: uuid.UUID, status: credentials.CredentialStatus) -> None:
        stmt = (
            sa.update(MachineCredentialRow)
            .where(MachineCredentialRow.id == credential_id)
            .values(status=MachineCredentialStatus(status.value))
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def registration_token(self, credential_id: uuid.UUID) -> str:
        """The credential's registration token, opened; ``""`` when none is stored.

        Raises ``RegistrationTokenUnreadable`` when it does not open under any configured key.
        """
        cipher = self._token_cipher()
        stmt = sa.select(MachineCredentialRow.registration_token).where(MachineCredentialRow.id == credential_id)
        return cipher.open((await self._session.execute(stmt)).scalar_one_or_none() or "")

    async def delete(self, credential_id: uuid.UUID) -> bool:
        """Delete the credential and its roles. True when a row was deleted."""
        result = await self._session.execute(
            sa.delete(MachineCredentialRow).where(MachineCredentialRow.id == credential_id)
        )
        await self._session.flush()
        return result.rowcount > 0

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
