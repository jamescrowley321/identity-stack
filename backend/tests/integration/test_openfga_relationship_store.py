"""The relationship-store contract against a real OpenFGA: the compose ``openfga-test`` server.

``make test-integration`` starts it (``make test-up-openfga``) and passes its URL as
``TEST_OPENFGA_URL``. Each test module run creates a store of its own and writes
``infra/openfga/model.json`` to it, the model ``infra/openfga`` applies, so runs
never share tuples and the model under test is the shipped one.
"""

import json
import os
import uuid
from pathlib import Path

import httpx
import pytest

from app.services.openfga import (
    MODEL_IN_TERRAFORM,
    OpenFgaApi,
    OpenFgaRelationshipStore,
    OpenFgaSettings,
    connect_openfga,
)
from app.services.relationship_store import (
    Relation,
    RelationshipStoreError,
    RelationshipStoreErrorKind,
    RelationshipStoreNotAvailable,
)
from tests.helpers.relationship_store_contract import DOCUMENT, FreshIds, RelationshipStoreContract

MODEL_JSON = Path(__file__).resolve().parents[3] / "infra" / "openfga" / "model.json"


@pytest.fixture(scope="module")
def openfga_url() -> str:
    url = os.environ.get("TEST_OPENFGA_URL", "")
    if not url:
        pytest.fail("TEST_OPENFGA_URL is not set: run the suite with `make test-integration`")
    try:
        httpx.get(f"{url}/healthz", timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        pytest.fail(f"openfga-test is not answering at {url} ({exc}): run `make test-up-openfga`")
    return url


@pytest.fixture(scope="module")
def settings(openfga_url) -> OpenFgaSettings:
    """A fresh store holding the shipped model."""
    store = httpx.post(f"{openfga_url}/stores", json={"name": f"contract-{uuid.uuid4()}"}, timeout=10)
    store.raise_for_status()
    store_id = store.json()["id"]
    model = httpx.post(
        f"{openfga_url}/stores/{store_id}/authorization-models", json=json.loads(MODEL_JSON.read_text()), timeout=10
    )
    model.raise_for_status()
    yield OpenFgaSettings(openfga_url, store_id, model.json()["authorization_model_id"])
    httpx.delete(f"{openfga_url}/stores/{store_id}", timeout=10)


@pytest.fixture
async def api(settings):
    async with httpx.AsyncClient(timeout=10) as http:
        yield OpenFgaApi(settings, http)


@pytest.fixture
def store(api) -> OpenFgaRelationshipStore:
    return OpenFgaRelationshipStore(api)


class TestOpenFgaContract(RelationshipStoreContract):
    """Every test of the contract, against OpenFGA."""


# OpenFGA's default OPENFGA_LIST_OBJECTS_MAX_RESULTS: ListObjects stops here without saying so.
LIST_OBJECTS_MAX_RESULTS = 1000
# The most tuples one Write takes (OPENFGA_MAX_TUPLES_PER_WRITE's default).
TUPLES_PER_WRITE = 100


class TestOpenFgaOnly(FreshIds):
    async def test_objects_lists_every_document_past_the_list_objects_cap(self, api, store, ids):
        """ListObjects would answer 1000 of these; every one the subject can view comes back."""
        granted = sorted(str(uuid.uuid4()) for _ in range(LIST_OBJECTS_MAX_RESULTS + 1))
        not_granted = sorted(str(uuid.uuid4()) for _ in range(3))
        for start in range(0, len(granted), TUPLES_PER_WRITE):
            await api.call(
                "POST",
                "/write",
                {
                    "authorization_model_id": api.settings.authorization_model_id,
                    "writes": {
                        "tuple_keys": [
                            {
                                "user": f"user:{ids['alice']}",
                                "relation": "viewer",
                                "object": f"document:{ids['tenant']}/{doc}",
                            }
                            for doc in granted[start : start + TUPLES_PER_WRITE]
                        ]
                    },
                },
            )

        held = await store.objects(
            ids["tenant"], DOCUMENT, "can_view", ids["alice"], candidates=sorted(granted + not_granted)
        )

        assert sorted(held) == granted

    async def test_writing_and_deleting_are_idempotent(self, store, ids):
        for _ in range(2):
            await store.write(ids["tenant"], DOCUMENT, ids["doc"], "viewer", ids["alice"])
        assert await store.all_relations(ids["tenant"], DOCUMENT, ids["doc"]) == [Relation("viewer", ids["alice"])]

        for _ in range(2):
            await store.delete(ids["tenant"], DOCUMENT, ids["doc"], "viewer", ids["alice"])
        assert await store.all_relations(ids["tenant"], DOCUMENT, ids["doc"]) == []

    async def test_a_subject_is_always_typed_user_and_never_passed_through(self, store, ids):
        """``user:<id>`` is not taken as already typed: the store types every subject itself."""
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], "viewer", ids["alice"])

        assert await store.check(ids["tenant"], DOCUMENT, ids["doc"], "can_view", ids["alice"]) is True
        with pytest.raises(RelationshipStoreError) as caught:
            await store.check(ids["tenant"], DOCUMENT, ids["doc"], "can_view", f"user:{ids['alice']}")
        assert caught.value.kind is RelationshipStoreErrorKind.REFUSED

    async def test_a_relation_the_model_does_not_define_is_refused(self, store, ids):
        with pytest.raises(RelationshipStoreError) as caught:
            await store.write(ids["tenant"], DOCUMENT, ids["doc"], "admin", ids["alice"])

        assert caught.value.kind is RelationshipStoreErrorKind.REFUSED
        assert caught.value.detail.startswith("Validation error: ")

    async def test_the_schema_is_the_shipped_model_and_cannot_be_written(self, store):
        schema = json.loads(await store.read_schema())

        assert {t["type"] for t in schema["type_definitions"]} == {"user", "document"}
        with pytest.raises(RelationshipStoreError) as caught:
            await store.write_schema("model\n  schema 1.1")
        assert (caught.value.kind, caught.value.detail) == (
            RelationshipStoreErrorKind.SCHEMA_READ_ONLY,
            MODEL_IN_TERRAFORM,
        )

    async def test_startup_reads_the_configured_model(self, settings, monkeypatch):
        monkeypatch.setenv("OPENFGA_API_URL", settings.api_url)
        monkeypatch.setenv("OPENFGA_STORE_ID", settings.store_id)
        monkeypatch.setenv("OPENFGA_AUTHORIZATION_MODEL_ID", settings.authorization_model_id)
        async with httpx.AsyncClient(timeout=10) as http:
            assert (await connect_openfga(http)).settings == settings

            monkeypatch.setenv("OPENFGA_AUTHORIZATION_MODEL_ID", "01ARZ3NDEKTSV4RRFFQ69G5FAV")
            with pytest.raises(RelationshipStoreNotAvailable, match="01ARZ3NDEKTSV4RRFFQ69G5FAV"):
                await connect_openfga(http)
