"""The relationship store a deployment checks fine-grained relations in.

``RelationshipStore`` is the one interface the FGA and document routes use.
Objects and subjects are given by the ids the routes hold: the tenant id from the
tenant resolver, the object's own id and the subject's user id. How a store keys
them is its own business: Descope's prefixes the object id with the tenant id,
OpenFGA's (``app.services.openfga``) names the tenant in the object id too.
This module is the only one that calls the Descope client's FGA methods
(``tests/unit/test_relationship_store.py``).

``RELATIONSHIP_STORE`` selects the store (``relationship_store_kind`` in
``app.dependencies.identity``); ``GET /api/capabilities`` reports it, and the FGA
and document routes answer 404 without one (``app.dependencies.capabilities``).

A store reports every failure as ``RelationshipStoreError``, whose ``kind`` says
what the routes answer: no transport error leaves a store.
"""

from __future__ import annotations

import enum
import json
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

from opentelemetry import trace

from app.outbound import UpstreamStatusError, UpstreamTransportError
from app.services.descope import DescopeManagementClient

tracer = trace.get_tracer(__name__)


class RelationshipStoreKind(enum.StrEnum):
    """The values ``RELATIONSHIP_STORE`` takes. ``none`` serves no FGA or document route."""

    # Descope's FGA store, keyed on Descope tenant and user ids.
    DESCOPE = "descope"
    # OpenFGA, keyed on canonical tenant and user ids.
    OPENFGA = "openfga"
    NONE = "none"


class RelationshipStoreFault(enum.StrEnum):
    # RELATIONSHIP_STORE names no store.
    UNKNOWN = "unknown"
    # A setting the store needs is unset.
    MISSING_SETTING = "missing_setting"
    # The store did not answer, or does not hold the configured store or model.
    UNREACHABLE = "unreachable"


class RelationshipStoreNotAvailable(ValueError):
    """The deployment's relationship store cannot be served: startup stops."""

    def __init__(self, fault: RelationshipStoreFault, selected: str, detail: str = "") -> None:
        self.fault = fault
        self.selected = selected
        if fault is RelationshipStoreFault.UNKNOWN:
            message = f"RELATIONSHIP_STORE {selected!r} is not one of {sorted(RelationshipStoreKind)}"
        else:
            message = f"The {selected} relationship store cannot be used: {detail}"
        super().__init__(message)


class RelationshipStoreErrorKind(enum.StrEnum):
    """What went wrong in a store call, and so what a route answers."""

    # The identifiers fail the store's own syntax: the caller's malformed request (422).
    INVALID = "invalid"
    # The store refused the request, e.g. a relation its schema does not define (400).
    REFUSED = "refused"
    # The store says what the call names is not there (Descope's 404). A revoke takes
    # it as done; every other route answers 502, as for UNAVAILABLE.
    ABSENT = "absent"
    # The store did not answer, or answered with something unusable (502).
    UNAVAILABLE = "unavailable"
    # The schema is not written through this service (409).
    SCHEMA_READ_ONLY = "schema_read_only"


class RelationshipStoreError(Exception):
    """A failed store call. ``detail`` is safe to answer; ``upstream`` is for the log only."""

    def __init__(self, kind: RelationshipStoreErrorKind, detail: str, upstream: str = "") -> None:
        self.kind = kind
        self.detail = detail
        self.upstream = upstream
        super().__init__(detail)


def _refusal_detail(body: str) -> str:
    """A store's refusal as the routes answer it: its ``message``, cut to 200 characters."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return f"Validation error: {data['message'][:200]}"
    return f"Validation error: {body[:200]}"


@dataclass(frozen=True)
class Relation:
    """A relation held on an object: ``subject`` holds ``relation``."""

    relation: str
    subject: str


class RelationshipStore(Protocol):
    """Check, write and list relations between subjects and the objects of a tenant, and read or write the schema.

    Every method raises ``RelationshipStoreError`` and nothing else for a failed call.
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
        """Replace the schema, or raise ``SCHEMA_READ_ONLY`` where it is managed elsewhere."""
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
    async def objects(
        self, tenant_id: str, object_type: str, relation: str, subject: str, *, candidates: Sequence[str]
    ) -> list[str]:
        """Those of ``candidates`` (the ids of the tenant's objects) on which ``subject`` holds ``relation``.

        The candidates come from the caller's own records, so the answer is every
        one of them the subject holds the relation on, never a store's truncated list.
        """
        ...


def _prefix_resource_id(tenant_id: str, resource_id: str) -> str:
    """Descope's key for a tenant's object: the object id prefixed with the tenant id."""
    return f"{tenant_id}:{resource_id}"


@contextmanager
def _descope_errors() -> Iterator[None]:
    """Descope's failures as ``RelationshipStoreError``s."""
    try:
        yield
    except json.JSONDecodeError as exc:
        # Before ValueError, which it subclasses: an unparseable body is Descope's fault.
        raise RelationshipStoreError(
            RelationshipStoreErrorKind.UNAVAILABLE, "Descope returned an unparseable response", str(exc)
        ) from exc
    except ValueError as exc:
        # The client checks FGA identifiers itself: a malformed request.
        raise RelationshipStoreError(RelationshipStoreErrorKind.INVALID, str(exc)) from exc
    except UpstreamStatusError as exc:
        status = exc.response.status_code
        upstream = f"HTTP {status} {exc.response.text[:500]}"
        if status == 400:
            raise RelationshipStoreError(
                RelationshipStoreErrorKind.REFUSED, _refusal_detail(exc.response.text), upstream
            ) from exc
        kind = RelationshipStoreErrorKind.ABSENT if status == 404 else RelationshipStoreErrorKind.UNAVAILABLE
        raise RelationshipStoreError(kind, "Descope's FGA store refused the call", upstream) from exc
    except UpstreamTransportError as exc:
        raise RelationshipStoreError(
            RelationshipStoreErrorKind.UNAVAILABLE, "Failed to reach Descope's FGA store", type(exc).__name__
        ) from exc


class DescopeRelationshipStore:
    """Descope's FGA store. An object is keyed ``<tenant id>:<object id>``, so tenants never share one."""

    def __init__(self, client: DescopeManagementClient) -> None:
        self._client = client

    async def check(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> bool:
        with (
            tracer.start_as_current_span(
                "DescopeRelationshipStore.check", record_exception=False, set_status_on_exception=False
            ),
            _descope_errors(),
        ):
            return bool(
                await self._client.check_permission(
                    object_type, _prefix_resource_id(tenant_id, object_id), relation, subject
                )
            )

    async def write(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> None:
        with (
            tracer.start_as_current_span(
                "DescopeRelationshipStore.write", record_exception=False, set_status_on_exception=False
            ),
            _descope_errors(),
        ):
            await self._client.create_relation(
                object_type, _prefix_resource_id(tenant_id, object_id), relation, subject
            )

    async def delete(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> None:
        with (
            tracer.start_as_current_span(
                "DescopeRelationshipStore.delete", record_exception=False, set_status_on_exception=False
            ),
            _descope_errors(),
        ):
            await self._client.delete_relation(
                object_type, _prefix_resource_id(tenant_id, object_id), relation, subject
            )

    async def read_schema(self) -> str:
        with (
            tracer.start_as_current_span(
                "DescopeRelationshipStore.read_schema", record_exception=False, set_status_on_exception=False
            ),
            _descope_errors(),
        ):
            # The AuthZ 1.0 DSL string: the text infra/fga.tf declares and write_schema takes back.
            return await self._client.get_fga_schema() or ""

    async def write_schema(self, schema: str) -> None:
        with (
            tracer.start_as_current_span(
                "DescopeRelationshipStore.write_schema", record_exception=False, set_status_on_exception=False
            ),
            _descope_errors(),
        ):
            await self._client.update_fga_schema(schema)

    async def relations(
        self, tenant_id: str, object_type: str, object_id: str, relation: str | None = None
    ) -> list[Relation]:
        with (
            tracer.start_as_current_span(
                "DescopeRelationshipStore.relations", record_exception=False, set_status_on_exception=False
            ),
            _descope_errors(),
        ):
            found = await self._client.list_relations(
                object_type, _prefix_resource_id(tenant_id, object_id), relation=relation
            )
            return _relations(found)

    async def all_relations(self, tenant_id: str, object_type: str, object_id: str) -> list[Relation]:
        with (
            tracer.start_as_current_span(
                "DescopeRelationshipStore.all_relations", record_exception=False, set_status_on_exception=False
            ),
            _descope_errors(),
        ):
            found = await self._client.list_all_relations(object_type, _prefix_resource_id(tenant_id, object_id))
            return _relations(found)

    async def objects(
        self, tenant_id: str, object_type: str, relation: str, subject: str, *, candidates: Sequence[str]
    ) -> list[str]:
        with (
            tracer.start_as_current_span(
                "DescopeRelationshipStore.objects", record_exception=False, set_status_on_exception=False
            ),
            _descope_errors(),
        ):
            # Descope answers for every tenant the subject holds the relation in: keep this tenant's.
            prefix = f"{tenant_id}:"
            keys = [
                r["resource"]
                for r in await self._client.list_user_resources(object_type, relation, subject) or []
                if isinstance(r, dict) and isinstance(r.get("resource"), str)
            ]
            held = {key[len(prefix) :] for key in keys if key.startswith(prefix)}
            return [object_id for object_id in candidates if object_id in held]


def _relations(found: list | None) -> list[Relation]:
    """Descope's relation dicts as ``Relation``s. One without a non-empty relation and target names nothing: skipped."""
    return [
        Relation(relation=r["relationDefinition"], subject=r["target"])
        for r in found or []
        if isinstance(r, dict) and _named(r.get("relationDefinition")) and _named(r.get("target"))
    ]


def _named(value: object) -> bool:
    return isinstance(value, str) and bool(value)
