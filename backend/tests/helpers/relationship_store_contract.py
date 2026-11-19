"""The relationship-store contract: what every ``RelationshipStore`` does, whichever store it is.

A test class for a store subclasses ``RelationshipStoreContract`` and provides a
``store`` fixture whose schema is the document model of ``infra/fga.tf`` and
``infra/openfga/model.json``: ``owner``, ``editor`` and ``viewer``, with
``can_view``, ``can_edit`` and ``can_delete`` derived from them. Descope's store
runs it in the unit suite over a fake of the client's FGA methods
(``tests/unit/test_relationship_store.py``); OpenFGA's runs it against the compose
server (``tests/integration/test_openfga_relationship_store.py``).

Every test names fresh tenant, object and subject ids, so the suite can run
against a store that holds other tuples.
"""

import uuid

import pytest

from app.services.relationship_store import Relation, RelationshipStore

DOCUMENT = "document"


def _id() -> str:
    return str(uuid.uuid4())


class FreshIds:
    """An ``ids`` fixture: fresh tenant, object and subject ids for each test."""

    @pytest.fixture
    def ids(self) -> dict[str, str]:
        return {name: _id() for name in ("tenant", "other_tenant", "doc", "other_doc", "alice", "bob")}


class RelationshipStoreContract(FreshIds):
    async def test_a_written_relation_holds(self, store: RelationshipStore, ids):
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], "owner", ids["alice"])

        assert await store.check(ids["tenant"], DOCUMENT, ids["doc"], "owner", ids["alice"]) is True
        assert await store.check(ids["tenant"], DOCUMENT, ids["doc"], "owner", ids["bob"]) is False

    @pytest.mark.parametrize(
        ("relation", "derived"),
        [
            ("owner", {"can_view": True, "can_edit": True, "can_delete": True}),
            ("editor", {"can_view": True, "can_edit": True, "can_delete": False}),
            ("viewer", {"can_view": True, "can_edit": False, "can_delete": False}),
        ],
    )
    async def test_permissions_derive_from_relations(self, store: RelationshipStore, ids, relation, derived):
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], relation, ids["alice"])

        checked = {
            permission: await store.check(ids["tenant"], DOCUMENT, ids["doc"], permission, ids["alice"])
            for permission in derived
        }

        assert checked == derived

    async def test_a_deleted_relation_no_longer_holds(self, store: RelationshipStore, ids):
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], "viewer", ids["alice"])
        await store.delete(ids["tenant"], DOCUMENT, ids["doc"], "viewer", ids["alice"])

        assert await store.check(ids["tenant"], DOCUMENT, ids["doc"], "can_view", ids["alice"]) is False
        assert await store.all_relations(ids["tenant"], DOCUMENT, ids["doc"]) == []

    async def test_the_relations_on_an_object_are_listed(self, store: RelationshipStore, ids):
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], "owner", ids["alice"])
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], "viewer", ids["bob"])
        await store.write(ids["tenant"], DOCUMENT, ids["other_doc"], "viewer", ids["alice"])

        listed = await store.all_relations(ids["tenant"], DOCUMENT, ids["doc"])
        narrowed = await store.relations(ids["tenant"], DOCUMENT, ids["doc"], relation="viewer")

        assert set(listed) == {Relation("owner", ids["alice"]), Relation("viewer", ids["bob"])}
        assert len(listed) == len(set(listed))
        assert narrowed == [Relation("viewer", ids["bob"])]

    async def test_objects_are_the_tenants_own_that_the_subject_holds_the_relation_on(
        self, store: RelationshipStore, ids
    ):
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], "owner", ids["alice"])
        await store.write(ids["tenant"], DOCUMENT, ids["other_doc"], "editor", ids["bob"])
        await store.write(ids["other_tenant"], DOCUMENT, ids["other_doc"], "viewer", ids["alice"])

        assert await store.objects(ids["tenant"], DOCUMENT, "can_view", ids["alice"]) == [ids["doc"]]
        assert await store.objects(ids["tenant"], DOCUMENT, "can_delete", ids["bob"]) == []

    async def test_tenants_never_share_an_object(self, store: RelationshipStore, ids):
        await store.write(ids["tenant"], DOCUMENT, ids["doc"], "owner", ids["alice"])

        assert await store.check(ids["other_tenant"], DOCUMENT, ids["doc"], "owner", ids["alice"]) is False
        assert await store.all_relations(ids["other_tenant"], DOCUMENT, ids["doc"]) == []
        assert await store.objects(ids["other_tenant"], DOCUMENT, "can_view", ids["alice"]) == []

    async def test_the_schema_reads_as_text_naming_the_document_relations(self, store: RelationshipStore):
        schema = await store.read_schema()

        assert isinstance(schema, str)
        assert {"document", "owner", "editor", "viewer", "can_view", "can_edit", "can_delete"} <= set(
            schema.replace('"', " ").replace(":", " ").replace(",", " ").split()
        )
