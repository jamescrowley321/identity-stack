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


class TestOpenFgaOnly(FreshIds):
    async def test_writing_and_deleting_are_idempotent(self, store, ids):
        for _ in range(2):
            await store.write(ids["tenant"], DOCUMENT, ids["doc"], "viewer", ids["alice"])
        assert await store.all_relations(ids["tenant"], DOCUMENT, ids["doc"]) == [Relation("viewer", ids["alice"])]

        for _ in range(2):
            await store.delete(ids["tenant"], DOCUMENT, ids["doc"], "viewer", ids["alice"])
        assert await store.all_relations(ids["tenant"], DOCUMENT, ids["doc"]) == []

    async def test_an_email_subject_is_a_user(self, store, ids):
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], "viewer", "someone@example.test")

        assert await store.check(ids["tenant"], DOCUMENT, ids["doc"], "can_view", "user:someone@example.test")
        assert await store.relations(ids["tenant"], DOCUMENT, ids["doc"]) == [
            Relation("viewer", "someone@example.test")
        ]

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
