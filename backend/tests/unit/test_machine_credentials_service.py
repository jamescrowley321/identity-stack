"""MachineCredentialService and the canonical machine credentials, with the repository and the push faked.

The router, the dependencies and the live Keycloak client are covered by
``tests/integration/test_keycloak_machine_credentials.py``.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import pytest
from expression import Error, Ok

from app.domain import credentials
from app.errors.identity import Conflict, GrantRefused, NotFound, ProviderError, SyncFailed, ValidationError
from app.repositories.base import RepositoryConflictError
from app.services.adapters.base import MachineClient, MachineClientRef, MachineCredentialPush, SyncError
from app.services.machine_credentials import CanonicalMachineCredentials, MachineCredentialService

NOW = 1_800_000_000
CREATED = datetime(2027, 1, 15, 8, 0, tzinfo=timezone.utc)
TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = uuid.UUID("22222222-2222-4222-8222-222222222222")
ADMIN_ROLE = uuid.UUID("33333333-3333-4333-8333-333333333333")
PROVIDER = "keycloak"
CLIENT = MachineClient(
    client_id="identity-stack-machine-x",
    external_id="kc-client",
    subject="kc-sub",
    secret="s3cret",
    registration_token="rat",
)
REFUSED = SyncError(message="refused", operation="op", context={"status": 403})


@dataclass
class FakeRepository:
    roles: dict[str, uuid.UUID] = field(default_factory=lambda: {"admin": ADMIN_ROLE})
    rows: dict[uuid.UUID, tuple[credentials.MachineCredential, str]] = field(default_factory=dict)
    conflict: bool = False
    commits: int = 0
    rollbacks: int = 0

    async def roles_named(self, tenant_id, names):
        return {name: role for name, role in self.roles.items() if name in names}

    async def create(self, credential, *, role_ids, registration_token):
        if self.conflict:
            raise RepositoryConflictError("taken")
        self.rows[credential.id] = (credential, registration_token)

    async def get(self, tenant_id, credential_id, *, provider):
        found = self.rows.get(credential_id)
        if found is None or found[0].tenant_id != tenant_id or found[0].provider != provider:
            return None
        return found[0]

    async def list_for_tenant(self, tenant_id, *, provider):
        return [credential for credential, _ in self.rows.values() if credential.tenant_id == tenant_id]

    async def set_status(self, credential_id, status):
        credential, token = self.rows[credential_id]
        self.rows[credential_id] = (replace(credential, status=status), token)

    async def registration_token(self, credential_id):
        return self.rows[credential_id][1]

    async def delete(self, credential_id):
        return self.rows.pop(credential_id, None) is not None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@dataclass
class FakePush(MachineCredentialPush):
    create_answer: object = field(default_factory=lambda: Ok(CLIENT))
    enable_answer: object = field(default_factory=lambda: Ok(None))
    delete_answer: object = field(default_factory=lambda: Ok(None))
    calls: list = field(default_factory=list)

    async def create_machine_client(self, *, credential_id, tenant_id, name):
        self.calls.append(("create", tenant_id, name))
        return self.create_answer

    async def set_machine_client_enabled(self, *, client: MachineClientRef, enabled: bool):
        self.calls.append(("enabled", client, enabled))
        return self.enable_answer

    async def delete_machine_client(self, *, client: MachineClientRef):
        self.calls.append(("delete", client))
        return self.delete_answer


def _service(repository: FakeRepository, push: FakePush) -> MachineCredentialService:
    store = CanonicalMachineCredentials(provider=PROVIDER, repository=repository, push=push, now=lambda: CREATED)
    return MachineCredentialService(store, clock=lambda: NOW)


async def _create(service: MachineCredentialService, **changes):
    arguments = {
        "tenant_id": str(TENANT),
        "name": "robot",
        "expire_time": NOW + 3600,
        "role_names": ["admin"],
        "caller_roles": ["admin"],
    }
    return await service.create(**{**arguments, **changes})


class TestCreate:
    async def test_a_credential_is_stored_and_its_secret_returned_once(self):
        repository, push = FakeRepository(), FakePush()

        result = await _create(_service(repository, push))

        (credential, token) = next(iter(repository.rows.values()))
        assert result == Ok(
            {
                "key": {
                    "id": str(credential.id),
                    "name": "robot",
                    "status": "active",
                    "clientId": CLIENT.client_id,
                    "roleNames": ["admin"],
                    "createdTime": int(CREATED.timestamp()),
                    "expireTime": NOW + 3600,
                },
                "cleartext": CLIENT.secret,
            }
        )
        assert (credential.tenant_id, credential.subject, credential.provider, token) == (
            TENANT,
            CLIENT.subject,
            PROVIDER,
            CLIENT.registration_token,
        )
        assert repository.commits == 1

    async def test_a_role_the_caller_lacks_is_refused_before_the_provider(self):
        repository, push = FakeRepository(), FakePush()

        result = await _create(_service(repository, push), role_names=["owner"])

        assert result == Error(GrantRefused(message="Cannot grant roles you do not hold: owner"))
        assert push.calls == []

    async def test_a_role_the_tenant_lacks_is_refused_before_the_provider(self):
        repository, push = FakeRepository(roles={}), FakePush()

        result = await _create(_service(repository, push))

        assert result == Error(ValidationError(message="The tenant has no roles named: admin"))
        assert push.calls == []

    async def test_an_unbounded_expiry_is_refused(self):
        push = FakePush()

        result = await _create(_service(FakeRepository(), push), expire_time=0)

        assert isinstance(result.error, ValidationError)
        assert push.calls == []

    async def test_a_provider_refusal_stores_nothing(self):
        repository, push = FakeRepository(), FakePush(create_answer=Error(REFUSED))

        result = await _create(_service(repository, push))

        assert isinstance(result.error, ProviderError)
        assert repository.rows == {}

    async def test_a_client_that_cannot_be_stored_is_deleted_again(self):
        repository, push = FakeRepository(conflict=True), FakePush()

        result = await _create(_service(repository, push))

        assert result == Error(Conflict(message="A machine credential for that client already exists"))
        assert [call[0] for call in push.calls] == ["create", "delete"]
        assert push.calls[-1][1].registration_token == CLIENT.registration_token
        assert repository.rollbacks == 1


async def _stored(repository: FakeRepository, push: FakePush) -> str:
    created = await _create(_service(repository, push))
    push.calls.clear()
    return created.ok["key"]["id"]


class TestStatusAndDelete:
    @pytest.mark.parametrize(
        ("operation", "status", "enabled"),
        [
            ("deactivate", credentials.CredentialStatus.INACTIVE, False),
            ("activate", credentials.CredentialStatus.ACTIVE, True),
        ],
    )
    async def test_the_status_is_stored_then_pushed(self, operation, status, enabled):
        repository, push = FakeRepository(), FakePush()
        key = await _stored(repository, push)

        result = await getattr(_service(repository, push), operation)(tenant_id=str(TENANT), credential_id=key)

        assert result == Ok({"status": f"{operation}d", "key_id": key})
        assert repository.rows[uuid.UUID(key)][0].status is status
        assert [(call[0], call[2]) for call in push.calls] == [("enabled", enabled)]

    async def test_a_status_the_provider_does_not_take_is_still_stored(self):
        """The canonical status decides at once whether its tokens are accepted."""
        repository, push = FakeRepository(), FakePush()
        key = await _stored(repository, push)
        push.enable_answer = Error(REFUSED)

        result = await _service(repository, push).deactivate(tenant_id=str(TENANT), credential_id=key)

        assert isinstance(result.error, SyncFailed)
        assert repository.rows[uuid.UUID(key)][0].status is credentials.CredentialStatus.INACTIVE

    async def test_delete_removes_the_client_with_its_registration_token_then_the_row(self):
        repository, push = FakeRepository(), FakePush()
        key = await _stored(repository, push)

        result = await _service(repository, push).delete(tenant_id=str(TENANT), credential_id=key)

        assert result == Ok({"status": "deleted", "key_id": key})
        assert repository.rows == {}
        assert [(call[0], call[1].registration_token) for call in push.calls] == [("delete", CLIENT.registration_token)]

    async def test_a_delete_the_provider_refuses_deactivates_and_keeps_the_credential(self):
        repository, push = FakeRepository(), FakePush()
        key = await _stored(repository, push)
        push.delete_answer = Error(REFUSED)

        result = await _service(repository, push).delete(tenant_id=str(TENANT), credential_id=key)

        assert isinstance(result.error, ProviderError)
        assert repository.rows[uuid.UUID(key)][0].status is credentials.CredentialStatus.INACTIVE

    @pytest.mark.parametrize("operation", ["get", "deactivate", "activate", "delete"])
    @pytest.mark.parametrize(
        "where",
        [
            lambda key: (str(OTHER_TENANT), key),
            lambda key: (str(TENANT), str(uuid.uuid4())),
            lambda key: (str(TENANT), "x"),
        ],
        ids=["another tenant", "no such credential", "not an id"],
    )
    async def test_only_the_tenants_own_credential_is_found(self, operation, where):
        repository, push = FakeRepository(), FakePush()
        key = await _stored(repository, push)
        tenant_id, credential_id = where(key)

        result = await getattr(_service(repository, push), operation)(tenant_id=tenant_id, credential_id=credential_id)

        assert result == Error(NotFound(message="Machine credential not found"))
        assert push.calls == []
        assert uuid.UUID(key) in repository.rows

    async def test_the_list_is_the_tenants(self):
        repository, push = FakeRepository(), FakePush()
        key = await _stored(repository, push)

        own = await _service(repository, push).list(tenant_id=str(TENANT))
        other = await _service(repository, push).list(tenant_id=str(OTHER_TENANT))

        assert [entry["id"] for entry in own.ok["keys"]] == [key]
        assert other == Ok({"keys": []})
