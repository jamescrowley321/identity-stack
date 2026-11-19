"""The relationship store a deployment checks fine-grained relations in.

``RelationshipStore`` is the one interface the FGA and document routes use.
Objects and subjects are given by the ids the routes hold: the tenant id from the
tenant resolver, the object's own id and the subject's user id. How a store keys
them is its own business: Descope's prefixes the object id with the tenant id.
This module is the only one that calls the Descope client's FGA methods
(``tests/unit/test_relationship_store.py``).

``RELATIONSHIP_STORE`` selects the store (``relationship_store_kind`` in
``app.dependencies.identity``); ``GET /api/capabilities`` reports it, and the FGA
and document routes answer 404 without one (``app.dependencies.capabilities``).
"""

from __future__ import annotations

import enum
from abc import abstractmethod
from dataclasses import dataclass
from typing import Protocol

from opentelemetry import trace

from app.services.descope import DescopeManagementClient

tracer = trace.get_tracer(__name__)


class RelationshipStoreKind(enum.StrEnum):
    # Descope's FGA store, keyed on Descope tenant and user ids.
    DESCOPE = "descope"
    # No store: the FGA and document routes are not served.
    NONE = "none"


class RelationshipStoreSetting(enum.StrEnum):
    """The values ``RELATIONSHIP_STORE`` takes."""

    DESCOPE = "descope"
    # OpenFgaRelationshipStore: identity-stack#682. Selecting it stops startup until then.
    OPENFGA = "openfga"


class RelationshipStoreFault(enum.StrEnum):
    UNKNOWN = "unknown"
    NOT_BUILT = "not_built"


class RelationshipStoreNotAvailable(ValueError):
    """``RELATIONSHIP_STORE`` names a store this build cannot serve: startup stops."""

    def __init__(self, fault: RelationshipStoreFault, selected: str) -> None:
        self.fault = fault
        self.selected = selected
        if fault is RelationshipStoreFault.NOT_BUILT:
            message = f"RELATIONSHIP_STORE={selected} names a relationship store this build does not implement"
        else:
            message = f"RELATIONSHIP_STORE {selected!r} is not one of {sorted(RelationshipStoreSetting)}"
        super().__init__(message)


@dataclass(frozen=True)
class Relation:
    """A relation held on an object: ``subject`` holds ``relation``."""

    relation: str
    subject: str


class RelationshipStore(Protocol):
    """Check, write and list relations between subjects and the objects of a tenant, and read or write the schema.

    Errors are the store's transport's: ``UpstreamStatusError`` and
    ``UpstreamTransportError`` for a failed call, ``ValueError`` for an identifier
    the store refuses.
    """

    @abstractmethod
    async def check(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> bool:
        """Whether ``subject`` holds ``relation`` (direct or derived) on the object."""
        ...

    @abstractmethod
    async def write(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> None:
        """Grant ``subject`` ``relation`` on the object."""
        ...

    @abstractmethod
    async def delete(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> None:
        """Remove ``subject``'s ``relation`` on the object."""
        ...

    @abstractmethod
    async def read_schema(self) -> str:
        """The schema, as text. It belongs to the whole store, not to a tenant."""
        ...

    @abstractmethod
    async def write_schema(self, schema: str) -> None:
        """Replace the schema."""
        ...

    @abstractmethod
    async def relations(
        self, tenant_id: str, object_type: str, object_id: str, relation: str | None = None
    ) -> list[Relation]:
        """The relations held on the object, narrowed to ``relation`` when given."""
        ...

    @abstractmethod
    async def all_relations(self, tenant_id: str, object_type: str, object_id: str) -> list[Relation]:
        """Every relation held on the object, to remove them all."""
        ...

    @abstractmethod
    async def objects(self, tenant_id: str, object_type: str, relation: str, subject: str) -> list[str]:
        """The ids of the tenant's objects on which ``subject`` holds ``relation``."""
        ...


def _prefix_resource_id(tenant_id: str, resource_id: str) -> str:
    """Descope's key for a tenant's object: the object id prefixed with the tenant id."""
    return f"{tenant_id}:{resource_id}"


class DescopeRelationshipStore:
    """Descope's FGA store. An object is keyed ``<tenant id>:<object id>``, so tenants never share one."""

    def __init__(self, client: DescopeManagementClient) -> None:
        self._client = client

    async def check(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> bool:
        with tracer.start_as_current_span(
            "DescopeRelationshipStore.check", record_exception=False, set_status_on_exception=False
        ):
            return bool(
                await self._client.check_permission(
                    object_type, _prefix_resource_id(tenant_id, object_id), relation, subject
                )
            )

    async def write(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> None:
        with tracer.start_as_current_span(
            "DescopeRelationshipStore.write", record_exception=False, set_status_on_exception=False
        ):
            await self._client.create_relation(
                object_type, _prefix_resource_id(tenant_id, object_id), relation, subject
            )

    async def delete(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> None:
        with tracer.start_as_current_span(
            "DescopeRelationshipStore.delete", record_exception=False, set_status_on_exception=False
        ):
            await self._client.delete_relation(
                object_type, _prefix_resource_id(tenant_id, object_id), relation, subject
            )

    async def read_schema(self) -> str:
        with tracer.start_as_current_span(
            "DescopeRelationshipStore.read_schema", record_exception=False, set_status_on_exception=False
        ):
            # The AuthZ 1.0 DSL string: the text infra/fga.tf declares and write_schema takes back.
            return await self._client.get_fga_schema() or ""

    async def write_schema(self, schema: str) -> None:
        with tracer.start_as_current_span(
            "DescopeRelationshipStore.write_schema", record_exception=False, set_status_on_exception=False
        ):
            await self._client.update_fga_schema(schema)

    async def relations(
        self, tenant_id: str, object_type: str, object_id: str, relation: str | None = None
    ) -> list[Relation]:
        with tracer.start_as_current_span(
            "DescopeRelationshipStore.relations", record_exception=False, set_status_on_exception=False
        ):
            found = await self._client.list_relations(
                object_type, _prefix_resource_id(tenant_id, object_id), relation=relation
            )
            return _relations(found)

    async def all_relations(self, tenant_id: str, object_type: str, object_id: str) -> list[Relation]:
        with tracer.start_as_current_span(
            "DescopeRelationshipStore.all_relations", record_exception=False, set_status_on_exception=False
        ):
            found = await self._client.list_all_relations(object_type, _prefix_resource_id(tenant_id, object_id))
            return _relations(found)

    async def objects(self, tenant_id: str, object_type: str, relation: str, subject: str) -> list[str]:
        with tracer.start_as_current_span(
            "DescopeRelationshipStore.objects", record_exception=False, set_status_on_exception=False
        ):
            # Descope answers for every tenant the subject holds the relation in: keep this tenant's.
            prefix = f"{tenant_id}:"
            keys = [
                r["resource"]
                for r in await self._client.list_user_resources(object_type, relation, subject) or []
                if isinstance(r, dict) and isinstance(r.get("resource"), str)
            ]
            return [key[len(prefix) :] for key in keys if key.startswith(prefix)]


def _relations(found: list | None) -> list[Relation]:
    """Descope's relation dicts as ``Relation``s. One without a non-empty relation and target names nothing: skipped."""
    return [
        Relation(relation=r["relationDefinition"], subject=r["target"])
        for r in found or []
        if isinstance(r, dict) and _named(r.get("relationDefinition")) and _named(r.get("target"))
    ]


def _named(value: object) -> bool:
    return isinstance(value, str) and bool(value)
