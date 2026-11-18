"""The Keycloak sync adapter's canonical side in memory, for tests that need no database."""

import uuid
from dataclasses import dataclass, field


@dataclass
class MemoryKeycloakLinks:
    """The canonical side in memory: which tenant names which organization, and the IdP links."""

    aliases: dict[uuid.UUID, str] = field(default_factory=dict)
    elsewhere: set[uuid.UUID] = field(default_factory=set)
    subjects: dict[uuid.UUID, str] = field(default_factory=dict)
    linked_users: dict[uuid.UUID, tuple[str, str]] = field(default_factory=dict)
    failure: Exception | None = None

    def _fail(self) -> None:
        if self.failure is not None:
            raise self.failure

    async def linked_subject(self, user_id: uuid.UUID) -> str | None:
        self._fail()
        return self.subjects.get(user_id)

    async def has_link(self, user_id: uuid.UUID) -> bool:
        self._fail()
        return user_id in self.subjects or user_id in self.linked_users

    async def link_user(self, user_id: uuid.UUID, sub: str, email: str) -> None:
        self._fail()
        self.linked_users[user_id] = (sub, email)

    async def organization_alias(self, tenant_id: uuid.UUID) -> str | None:
        self._fail()
        return self.aliases.get(tenant_id)

    async def linked_elsewhere(self, tenant_id: uuid.UUID) -> bool:
        self._fail()
        return tenant_id in self.elsewhere

    async def link_organization(self, tenant_id: uuid.UUID, alias: str) -> None:
        self._fail()
        self.aliases[tenant_id] = alias
