"""The ``machine_credentials`` capability: credentials a machine signs in with, owned by a tenant.

``MachineCredentialService`` is what ``/api/keys`` calls. It checks what a new
credential may be (``app.domain.credentials``) and hands the request to the
deployment's ``MachineCredentials``, chosen by the sync provider
(``app.dependencies.identity``):

- ``DescopeAccessKeys``: Descope access keys, through the management adapter.
  Descope keeps the keys; nothing is stored here.
- ``CanonicalMachineCredentials``: a canonical provider's. Each credential is a
  ``machine_credentials`` row, which authorizes its tokens (conventions §18), and
  a client the sync adapter keeps in the provider (``MachineCredentialPush``).

Both answer in the access-key shapes the SPA reads: ``{"key": ..., "cleartext":
...}`` on create, ``{"keys": [...]}`` on list, and each key with ``id``, ``name``
and ``status``.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone

from expression import Error, Ok, Result
from opentelemetry import trace

from app.domain import credentials
from app.errors.credentials import identity_error
from app.errors.identity import Conflict, IdentityError, NotFound, ProviderError, SyncFailed
from app.logbook import logbook
from app.repositories.base import RepositoryConflictError
from app.repositories.machine_credential import MachineCredentialRepository
from app.repositories.registration_tokens import RegistrationTokenNotSealed, RegistrationTokenUnreadable
from app.services.adapters.base import MachineClient, MachineClientRef, MachineCredentialPush, SyncError
from app.services.descope_management import DescopeManagementAdapter

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

DEACTIVATED = "deactivated"
ACTIVATED = "activated"
DELETED = "deleted"


class MachineCredentials(ABC):
    """One deployment's machine credentials, each operation within the caller's tenant."""

    @abstractmethod
    async def create(
        self, *, tenant_id: str, name: str, expire_time: int, role_names: tuple[str, ...]
    ) -> Result[dict, IdentityError]:
        """Create a credential. The value carries its secret (``cleartext``), shown once."""

    @abstractmethod
    async def list(self, *, tenant_id: str) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def get(self, *, tenant_id: str, credential_id: str) -> Result[dict, IdentityError]: ...

    @abstractmethod
    async def set_active(self, *, tenant_id: str, credential_id: str, active: bool) -> Result[None, IdentityError]: ...

    @abstractmethod
    async def delete(self, *, tenant_id: str, credential_id: str) -> Result[None, IdentityError]: ...


class DescopeAccessKeys(MachineCredentials):
    """Descope access keys, in the shapes Descope reports them."""

    def __init__(self, management: DescopeManagementAdapter) -> None:
        self._management = management

    async def create(
        self, *, tenant_id: str, name: str, expire_time: int, role_names: tuple[str, ...]
    ) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("DescopeAccessKeys.create"):
            return await self._management.create_access_key(
                name=name,
                tenant_id=tenant_id,
                expire_time=expire_time,
                role_names=list(role_names),
            )

    async def list(self, *, tenant_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("DescopeAccessKeys.list"):
            return await self._management.list_access_keys(tenant_id)

    async def get(self, *, tenant_id: str, credential_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("DescopeAccessKeys.get"):
            return await self._management.load_tenant_access_key(credential_id, tenant_id)

    async def set_active(self, *, tenant_id: str, credential_id: str, active: bool) -> Result[None, IdentityError]:
        with tracer.start_as_current_span("DescopeAccessKeys.set_active"):
            found = await self._management.load_tenant_access_key(credential_id, tenant_id)
            if found.is_error():
                return Error(found.error)
            if active:
                return await self._management.activate_access_key(credential_id)
            return await self._management.deactivate_access_key(credential_id)

    async def delete(self, *, tenant_id: str, credential_id: str) -> Result[None, IdentityError]:
        with tracer.start_as_current_span("DescopeAccessKeys.delete"):
            found = await self._management.load_tenant_access_key(credential_id, tenant_id)
            if found.is_error():
                return Error(found.error)
            return await self._management.delete_access_key(credential_id)


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _view(credential: credentials.MachineCredential) -> dict:
    """A canonical credential in the access-key shape the SPA reads."""
    return {
        "id": str(credential.id),
        "name": credential.name,
        "status": credential.status.value,
        "clientId": credential.client_id,
        "roleNames": list(credential.role_names),
        "createdTime": int(credential.created_at.timestamp()),
        "expireTime": int(credential.expires_at.timestamp()),
    }


def _ref(credential: credentials.MachineCredential, registration_token: str = "") -> MachineClientRef:
    return MachineClientRef(
        client_id=credential.client_id,
        external_id=credential.external_id,
        subject=credential.subject,
        registration_token=registration_token,
    )


# The answer to a delete whose stored registration token opens under no configured key.
UNREADABLE_REGISTRATION_TOKEN = ProviderError(
    message="The machine client's registration token opens under no configured key: the credential is "
    "deactivated, and a realm admin must remove its client"
)

# The answer to a delete whose stored registration token was never sealed: nothing is changed.
UNSEALED_REGISTRATION_TOKEN = ProviderError(
    message="The machine client's registration token is stored unsealed: run migration "
    "011_seal_registration_tokens, then delete again"
)

# The answer when the service account could not be disabled after an unreadable registration token.
UNREADABLE_REGISTRATION_TOKEN_DISABLE_FAILED = ProviderError(
    message=(
        "The machine client's registration token opens under no configured key and its service account "
        "could not be disabled in the identity provider: the credential is deactivated here and must be "
        "disabled or deleted in the provider"
    )
)


def _provider_error(operation: str, error: SyncError) -> ProviderError:
    logger.warning("machine clients: %s failed in the provider: %s", operation, error.message)
    return ProviderError(message=f"The identity provider refused {operation}", context=error.context)


class CanonicalMachineCredentials(MachineCredentials):
    """A canonical provider's machine credentials: ``machine_credentials`` rows, each pushed as a client."""

    def __init__(
        self,
        *,
        provider: str,
        repository: MachineCredentialRepository,
        push: MachineCredentialPush,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._provider = provider
        self._repository = repository
        self._push = push
        self._now = now

    async def _find(self, tenant_id: str, credential_id: str) -> Result[credentials.MachineCredential, IdentityError]:
        tenant, credential = _uuid(tenant_id), _uuid(credential_id)
        found = (
            None
            if tenant is None or credential is None
            else await self._repository.get(tenant, credential, provider=self._provider)
        )
        if found is None:
            return Error(identity_error(credentials.CredentialNotFound(credential_id=credential_id)))
        return Ok(found)

    async def create(
        self, *, tenant_id: str, name: str, expire_time: int, role_names: tuple[str, ...]
    ) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("CanonicalMachineCredentials.create"):
            tenant = _uuid(tenant_id)
            if tenant is None:
                return Error(NotFound(message="Tenant not found"))
            requested = role_names
            roles = await self._repository.roles_named(tenant, requested)
            known = credentials.known_roles(requested, found=frozenset(roles))
            if known.is_error():
                return Error(identity_error(known.error))
            credential_id = uuid.uuid4()
            pushed = await self._push.create_machine_client(credential_id=credential_id, tenant_id=tenant, name=name)
            if pushed.is_error():
                return Error(_provider_error("creating the machine client", pushed.error))
            client: MachineClient = pushed.ok
            credential = credentials.MachineCredential(
                id=credential_id,
                tenant_id=tenant,
                provider=self._provider,
                name=name,
                client_id=client.client_id,
                subject=client.subject,
                external_id=client.external_id,
                status=credentials.CredentialStatus.ACTIVE,
                expires_at=datetime.fromtimestamp(expire_time, tz=timezone.utc),
                created_at=self._now(),
                role_names=tuple(sorted(requested)),
            )
            try:
                await self._repository.create(
                    credential,
                    role_ids=tuple(roles[role] for role in requested),
                    registration_token=client.registration_token,
                )
                await self._repository.commit()
            except RepositoryConflictError:
                await self._undo(credential, client)
                return Error(Conflict(message="A machine credential for that client already exists"))
            except Exception:
                await self._undo(credential, client)
                raise
            return Ok({"key": _view(credential), "cleartext": client.secret})

    async def _undo(self, credential: credentials.MachineCredential, client: MachineClient) -> None:
        """Take back a client whose credential could not be stored, so no client is left that nothing authorizes."""
        await self._repository.rollback()
        undone = await self._push.delete_machine_client(client=_ref(credential, client.registration_token))
        if undone.is_error():
            logger.error(
                "machine clients: the client %s, whose row could not be stored, is left in the provider",
                client.client_id,
            )

    async def list(self, *, tenant_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("CanonicalMachineCredentials.list"):
            tenant = _uuid(tenant_id)
            found = [] if tenant is None else await self._repository.list_for_tenant(tenant, provider=self._provider)
            return Ok({"keys": [_view(credential) for credential in found]})

    async def get(self, *, tenant_id: str, credential_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("CanonicalMachineCredentials.get"):
            found = await self._find(tenant_id, credential_id)
            return Ok(_view(found.ok)) if found.is_ok() else Error(found.error)

    async def set_active(self, *, tenant_id: str, credential_id: str, active: bool) -> Result[None, IdentityError]:
        """The canonical status first, which decides at once whether its tokens are accepted; then the provider."""
        with tracer.start_as_current_span("CanonicalMachineCredentials.set_active"):
            found = await self._find(tenant_id, credential_id)
            if found.is_error():
                return Error(found.error)
            status = credentials.CredentialStatus.ACTIVE if active else credentials.CredentialStatus.INACTIVE
            await self._repository.set_status(found.ok.id, status)
            await self._repository.commit()
            pushed = await self._push.set_machine_client_enabled(client=_ref(found.ok), enabled=active)
            if pushed.is_error():
                logger.warning("machine clients: the provider did not take status %s: %s", status, pushed.error.message)
                return Error(
                    SyncFailed(
                        message="The credential's status is changed here, but not yet in the identity provider",
                        operation="set_machine_client_enabled",
                    )
                )
            return Ok(None)

    async def _retire_unreadable(self, credential: credentials.MachineCredential) -> Result[None, IdentityError]:
        """Disable the client without its registration token; drop the row only once the client is gone."""
        disabled = await self._push.set_machine_client_enabled(client=_ref(credential), enabled=False)
        if disabled.is_error():
            logger.warning(
                "machine clients: disabling client %s (#698) failed: %s",
                credential.client_id,
                disabled.error.message,
            )
        exists = await self._push.machine_client_exists(client=_ref(credential))
        if exists.is_error():
            logger.warning(
                "machine clients: could not confirm whether client %s is gone: %s",
                credential.client_id,
                exists.error.message,
            )
        elif not exists.ok:
            await self._repository.delete(credential.id)
            await self._repository.commit()
            return Ok(None)
        return Error(
            UNREADABLE_REGISTRATION_TOKEN_DISABLE_FAILED if disabled.is_error() else UNREADABLE_REGISTRATION_TOKEN
        )

    async def delete(self, *, tenant_id: str, credential_id: str) -> Result[None, IdentityError]:
        """The provider's client first, with the stored registration token, then the row.

        When the provider refuses, the credential is deactivated instead and kept,
        so its tokens are refused and the delete can be asked again. When the stored
        token opens under no configured key (#698), the credential is deactivated, its
        client's service account disabled, and the row kept until the provider proves
        the client gone (a realm admin removed it); the next delete then removes the row.
        """
        with tracer.start_as_current_span("CanonicalMachineCredentials.delete"):
            found = await self._find(tenant_id, credential_id)
            if found.is_error():
                return Error(found.error)
            try:
                token = await self._repository.registration_token(found.ok.id)
            except RegistrationTokenNotSealed:
                logger.warning(
                    "machine clients: %s left as it is; its stored value predates 011_seal_registration_tokens",
                    found.ok.id,
                )
                return Error(UNSEALED_REGISTRATION_TOKEN)
            except RegistrationTokenUnreadable:
                await self._repository.set_status(found.ok.id, credentials.CredentialStatus.INACTIVE)
                await self._repository.commit()
                logger.warning(
                    "machine clients: %s deactivated; a realm admin must remove client %s (#698)",
                    found.ok.id,
                    found.ok.client_id,
                )
                return await self._retire_unreadable(found.ok)
            pushed = await self._push.delete_machine_client(client=_ref(found.ok, token))
            if pushed.is_error():
                await self._repository.set_status(found.ok.id, credentials.CredentialStatus.INACTIVE)
                await self._repository.commit()
                return Error(_provider_error("deleting the machine client", pushed.error))
            await self._repository.delete(found.ok.id)
            await self._repository.commit()
            return Ok(None)


class MachineCredentialService:
    """``/api/keys``: what a new credential may be, then the deployment's ``MachineCredentials``."""

    def __init__(self, credentials_store: MachineCredentials, *, clock: Callable[[], float] = time.time) -> None:
        self._store = credentials_store
        self._clock = clock

    async def create(
        self,
        *,
        tenant_id: str,
        name: str,
        expire_time: int | None,
        role_names: list[str] | None,
        caller_roles: list[str],
    ) -> Result[dict, IdentityError]:
        """Create a credential carrying only roles the caller holds, with a bounded future expiry."""
        with tracer.start_as_current_span("MachineCredentialService.create"):
            requested = tuple(role_names or ())
            held = credentials.grantable_roles(requested, held=frozenset(caller_roles))
            if held.is_error():
                return Error(identity_error(held.error))
            bounded = credentials.expiry(expire_time, now=int(self._clock()))
            if bounded.is_error():
                return Error(identity_error(bounded.error))
            return await self._store.create(
                tenant_id=tenant_id,
                name=name,
                expire_time=bounded.ok,
                role_names=held.ok,
            )

    async def list(self, *, tenant_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("MachineCredentialService.list"):
            return await self._store.list(tenant_id=tenant_id)

    async def get(self, *, tenant_id: str, credential_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("MachineCredentialService.get"):
            return await self._store.get(tenant_id=tenant_id, credential_id=credential_id)

    async def deactivate(self, *, tenant_id: str, credential_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("MachineCredentialService.deactivate"):
            done = await self._store.set_active(tenant_id=tenant_id, credential_id=credential_id, active=False)
            return Ok({"status": DEACTIVATED, "key_id": credential_id}) if done.is_ok() else Error(done.error)

    async def activate(self, *, tenant_id: str, credential_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("MachineCredentialService.activate"):
            done = await self._store.set_active(tenant_id=tenant_id, credential_id=credential_id, active=True)
            return Ok({"status": ACTIVATED, "key_id": credential_id}) if done.is_ok() else Error(done.error)

    async def delete(self, *, tenant_id: str, credential_id: str) -> Result[dict, IdentityError]:
        with tracer.start_as_current_span("MachineCredentialService.delete"):
            done = await self._store.delete(tenant_id=tenant_id, credential_id=credential_id)
            return Ok({"status": DELETED, "key_id": credential_id}) if done.is_ok() else Error(done.error)
