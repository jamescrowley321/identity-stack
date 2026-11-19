"""OpenFGA as the relationship store, over its HTTP API.

The store and the authorization model are configuration (``OPENFGA_STORE_ID``,
``OPENFGA_AUTHORIZATION_MODEL_ID``): Terraform creates both (``infra/openfga``)
and every call names that model, so a model written by anyone else is never
used. The service never writes a model, so ``write_schema`` is refused
(``SCHEMA_READ_ONLY``).

Objects and subjects are canonical ids. An object is ``<type>:<tenant id>/<object id>``,
so tenants never share one, and a subject is ``user:<user id>``. Relations are
listed with the bare user id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace

from app.knobs import knob
from app.outbound import OutboundClient, UpstreamStatusError, UpstreamTransportError
from app.services.relationship_store import (
    Relation,
    RelationshipStoreError,
    RelationshipStoreErrorKind,
    RelationshipStoreFault,
    RelationshipStoreKind,
    RelationshipStoreNotAvailable,
    _refusal_detail,
)

tracer = trace.get_tracer(__name__)

OPENFGA_TIMEOUT_SECONDS = 10.0
# The largest page OpenFGA's Read API serves.
_READ_PAGE_SIZE = 100
SUBJECT_TYPE = "user"

MODEL_IN_TERRAFORM = "The OpenFGA authorization model is managed by Terraform (infra/openfga); change it there"


@dataclass(frozen=True)
class OpenFgaSettings:
    api_url: str
    store_id: str
    authorization_model_id: str
    api_token: str = field(default="", repr=False)


def _openfga_settings() -> OpenFgaSettings:
    """The OpenFGA settings, or ``RelationshipStoreNotAvailable`` naming the first one unset."""
    values = {
        "OPENFGA_API_URL": (knob("OPENFGA_API_URL") or "").strip().rstrip("/"),
        "OPENFGA_STORE_ID": (knob("OPENFGA_STORE_ID") or "").strip(),
        "OPENFGA_AUTHORIZATION_MODEL_ID": (knob("OPENFGA_AUTHORIZATION_MODEL_ID") or "").strip(),
    }
    for name, value in values.items():
        if not value:
            raise RelationshipStoreNotAvailable(
                RelationshipStoreFault.MISSING_SETTING, RelationshipStoreKind.OPENFGA, f"{name} is not set"
            )
    return OpenFgaSettings(
        api_url=values["OPENFGA_API_URL"],
        store_id=values["OPENFGA_STORE_ID"],
        authorization_model_id=values["OPENFGA_AUTHORIZATION_MODEL_ID"],
        api_token=(knob("OPENFGA_API_TOKEN") or "").strip(),
    )


class OpenFgaApi:
    """The process-wide OpenFGA client: one store, one model. Built and closed by ``main.lifespan``."""

    def __init__(self, settings: OpenFgaSettings, http: OutboundClient) -> None:
        self.settings = settings
        self._http = http

    def _headers(self) -> dict[str, str]:
        if self.settings.api_token:
            return {"Authorization": f"Bearer {self.settings.api_token}"}
        return {}

    def _url(self, path: str) -> str:
        return f"{self.settings.api_url}/stores/{self.settings.store_id}{path}"

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """One API call; every failure as a ``RelationshipStoreError``."""
        with tracer.start_as_current_span("OpenFgaApi.call", attributes={"openfga.path": path}):
            try:
                response = await self._http.request(method, self._url(path), json=body, headers=self._headers())
                response.raise_for_status()
                data = response.json()
            except UpstreamStatusError as exc:
                status = exc.response.status_code
                upstream = f"HTTP {status} {exc.response.text[:500]}"
                if status == 400:
                    raise RelationshipStoreError(
                        RelationshipStoreErrorKind.REFUSED, _refusal_detail(exc.response.text), upstream
                    ) from exc
                raise RelationshipStoreError(
                    RelationshipStoreErrorKind.UNAVAILABLE, "OpenFGA refused the call", upstream
                ) from exc
            except UpstreamTransportError as exc:
                raise RelationshipStoreError(
                    RelationshipStoreErrorKind.UNAVAILABLE, "Failed to reach OpenFGA", type(exc).__name__
                ) from exc
            except json.JSONDecodeError as exc:
                raise RelationshipStoreError(
                    RelationshipStoreErrorKind.UNAVAILABLE, "OpenFGA returned an unparseable response", str(exc)
                ) from exc
            if not isinstance(data, dict):
                raise RelationshipStoreError(
                    RelationshipStoreErrorKind.UNAVAILABLE,
                    "OpenFGA returned an unparseable response",
                    repr(data)[:200],
                )
            return data

    async def authorization_model(self) -> dict[str, Any]:
        """The configured authorization model, as OpenFGA's JSON."""
        with tracer.start_as_current_span("OpenFgaApi.authorization_model"):
            data = await self.call("GET", f"/authorization-models/{self.settings.authorization_model_id}")
            model = data.get("authorization_model")
            if not isinstance(model, dict):
                raise RelationshipStoreError(
                    RelationshipStoreErrorKind.UNAVAILABLE, "OpenFGA returned no authorization model", repr(data)[:200]
                )
            return model


async def connect_openfga(http: OutboundClient) -> OpenFgaApi:
    """Read the settings and the configured model once; stop startup when either is missing."""
    with tracer.start_as_current_span("openfga.connect_openfga"):
        api = OpenFgaApi(_openfga_settings(), http)
        try:
            await api.authorization_model()
        except RelationshipStoreError as exc:
            raise RelationshipStoreNotAvailable(
                RelationshipStoreFault.UNREACHABLE,
                RelationshipStoreKind.OPENFGA,
                f"reading authorization model {api.settings.authorization_model_id} of store "
                f"{api.settings.store_id} at {api.settings.api_url} failed: {exc.detail} ({exc.upstream})",
            ) from exc
        return api


def _object(object_type: str, tenant_id: str, object_id: str) -> str:
    return f"{object_type}:{tenant_id}/{object_id}"


def _subject(subject: str) -> str:
    """``user:<subject>``: every subject is a canonical user id."""
    return f"{SUBJECT_TYPE}:{subject}"


def _tuple(tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> dict[str, str]:
    return {"user": _subject(subject), "relation": relation, "object": _object(object_type, tenant_id, object_id)}


def _subject_id(user: object) -> str | None:
    """The user id of a tuple's ``user:<id>``; ``None`` for anything else."""
    prefix = f"{SUBJECT_TYPE}:"
    if isinstance(user, str) and user.startswith(prefix) and len(user) > len(prefix):
        return user[len(prefix) :]
    return None


class OpenFgaRelationshipStore:
    """``RelationshipStore`` over one OpenFGA store and authorization model."""

    def __init__(self, api: OpenFgaApi) -> None:
        self._api = api

    @property
    def _model_id(self) -> str:
        return self._api.settings.authorization_model_id

    async def check(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> bool:
        with tracer.start_as_current_span(
            "OpenFgaRelationshipStore.check", record_exception=False, set_status_on_exception=False
        ):
            data = await self._api.call(
                "POST",
                "/check",
                {
                    "authorization_model_id": self._model_id,
                    "tuple_key": _tuple(tenant_id, object_type, object_id, relation, subject),
                },
            )
            return data.get("allowed") is True

    async def write(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> None:
        with tracer.start_as_current_span(
            "OpenFgaRelationshipStore.write", record_exception=False, set_status_on_exception=False
        ):
            # A tuple that is already there is not an error: granting is idempotent.
            await self._api.call(
                "POST",
                "/write",
                {
                    "authorization_model_id": self._model_id,
                    "writes": {
                        "tuple_keys": [_tuple(tenant_id, object_type, object_id, relation, subject)],
                        "on_duplicate": "ignore",
                    },
                },
            )

    async def delete(self, tenant_id: str, object_type: str, object_id: str, relation: str, subject: str) -> None:
        with tracer.start_as_current_span(
            "OpenFgaRelationshipStore.delete", record_exception=False, set_status_on_exception=False
        ):
            # A tuple that is not there is not an error: revoking is idempotent.
            await self._api.call(
                "POST",
                "/write",
                {
                    "authorization_model_id": self._model_id,
                    "deletes": {
                        "tuple_keys": [_tuple(tenant_id, object_type, object_id, relation, subject)],
                        "on_missing": "ignore",
                    },
                },
            )

    async def read_schema(self) -> str:
        with tracer.start_as_current_span(
            "OpenFgaRelationshipStore.read_schema", record_exception=False, set_status_on_exception=False
        ):
            # OpenFGA's JSON form of the model infra/openfga/model.json declares.
            return json.dumps(await self._api.authorization_model(), indent=2, sort_keys=True)

    async def write_schema(self, schema: str) -> None:
        with tracer.start_as_current_span(
            "OpenFgaRelationshipStore.write_schema", record_exception=False, set_status_on_exception=False
        ):
            raise RelationshipStoreError(RelationshipStoreErrorKind.SCHEMA_READ_ONLY, MODEL_IN_TERRAFORM)

    async def relations(
        self, tenant_id: str, object_type: str, object_id: str, relation: str | None = None
    ) -> list[Relation]:
        with tracer.start_as_current_span(
            "OpenFgaRelationshipStore.relations", record_exception=False, set_status_on_exception=False
        ):
            return await self._read(tenant_id, object_type, object_id, relation)

    async def all_relations(self, tenant_id: str, object_type: str, object_id: str) -> list[Relation]:
        with tracer.start_as_current_span(
            "OpenFgaRelationshipStore.all_relations", record_exception=False, set_status_on_exception=False
        ):
            return await self._read(tenant_id, object_type, object_id, None)

    async def _read(self, tenant_id: str, object_type: str, object_id: str, relation: str | None) -> list[Relation]:
        """Every tuple on the object, page by page, as ``Relation``s of user subjects."""
        tuple_key: dict[str, str] = {"object": _object(object_type, tenant_id, object_id)}
        if relation is not None:
            tuple_key["relation"] = relation
        found: list[Relation] = []
        token = ""
        while True:
            body: dict[str, Any] = {"tuple_key": tuple_key, "page_size": _READ_PAGE_SIZE}
            if token:
                body["continuation_token"] = token
            data = await self._api.call("POST", "/read", body)
            for entry in data.get("tuples") or []:
                key = entry.get("key") if isinstance(entry, dict) else None
                if not isinstance(key, dict):
                    continue
                subject = _subject_id(key.get("user"))
                if subject is not None and isinstance(key.get("relation"), str) and key["relation"]:
                    found.append(Relation(relation=key["relation"], subject=subject))
            token = data.get("continuation_token") or ""
            if not isinstance(token, str) or not token:
                return found

    async def objects(self, tenant_id: str, object_type: str, relation: str, subject: str) -> list[str]:
        with tracer.start_as_current_span(
            "OpenFgaRelationshipStore.objects", record_exception=False, set_status_on_exception=False
        ):
            data = await self._api.call(
                "POST",
                "/list-objects",
                {
                    "authorization_model_id": self._model_id,
                    "type": object_type,
                    "relation": relation,
                    "user": _subject(subject),
                },
            )
            # OpenFGA answers for every tenant: keep this tenant's.
            prefix = f"{object_type}:{tenant_id}/"
            return [
                name[len(prefix) :]
                for name in data.get("objects") or []
                if isinstance(name, str) and name.startswith(prefix) and len(name) > len(prefix)
            ]
