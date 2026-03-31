"""Unit tests for the FGA demo seed script."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scripts.seed_demo import (
    DEMO_DOCUMENTS,
    _ensure_relation,
    _get_or_create_documents,
    _seed_fga_relations,
    main,
)

# ---------------------------------------------------------------------------
# _require_env
# ---------------------------------------------------------------------------


class TestRequireEnv:
    def test_missing_env_var_exits(self):
        """_require_env exits with code 1 when var is not set."""
        from scripts.seed_demo import _require_env

        with pytest.raises(SystemExit) as exc_info:
            _require_env("DEFINITELY_NOT_SET_XYZ")
        assert exc_info.value.code == 1

    def test_empty_env_var_exits(self, monkeypatch):
        """_require_env exits with code 1 when var is empty string."""
        from scripts.seed_demo import _require_env

        monkeypatch.setenv("EMPTY_VAR", "")
        with pytest.raises(SystemExit) as exc_info:
            _require_env("EMPTY_VAR")
        assert exc_info.value.code == 1

    def test_present_env_var_returns_value(self, monkeypatch):
        """_require_env returns the value when set."""
        from scripts.seed_demo import _require_env

        monkeypatch.setenv("SEED_TEST_VAR", "hello")
        assert _require_env("SEED_TEST_VAR") == "hello"


# ---------------------------------------------------------------------------
# _get_or_create_documents (async)
# ---------------------------------------------------------------------------


class TestGetOrCreateDocuments:
    @pytest.mark.anyio
    @patch("scripts.seed_demo.DATABASE_URL", "sqlite+aiosqlite://")
    async def test_creates_new_documents(self):
        """Creates all three demo documents when none exist."""
        docs = await _get_or_create_documents("tenant-1", "user-1")
        assert len(docs) == 3
        titles = {d.title for d in docs}
        assert titles == {"public-roadmap", "board-minutes", "team-project"}

    @pytest.mark.anyio
    async def test_skips_existing_documents(self, tmp_path):
        """Second call skips documents created by the first (idempotent)."""
        db_path = tmp_path / "seed_skip.db"
        db_url = f"sqlite+aiosqlite:///{db_path}"
        with patch("scripts.seed_demo.DATABASE_URL", db_url):
            docs1 = await _get_or_create_documents("tenant-1", "user-1")
            assert len(docs1) == 3
            # Second call hits the same file-backed DB and should skip
            docs2 = await _get_or_create_documents("tenant-1", "user-1")
            assert len(docs2) == 3
            # All documents should have the same IDs (skipped, not recreated)
            ids1 = {d.id for d in docs1}
            ids2 = {d.id for d in docs2}
            assert ids1 == ids2

    @pytest.mark.anyio
    @patch("scripts.seed_demo.DATABASE_URL", "sqlite+aiosqlite://")
    async def test_documents_have_correct_tenant_and_creator(self):
        """Created documents have correct tenant_id and created_by."""
        docs = await _get_or_create_documents("tenant-1", "user-1")
        for doc in docs:
            assert doc.tenant_id == "tenant-1"
            assert doc.created_by == "user-1"


# ---------------------------------------------------------------------------
# _ensure_relation
# ---------------------------------------------------------------------------


class TestEnsureRelation:
    @pytest.mark.anyio
    async def test_creates_relation_success(self):
        """Creates a relation when it doesn't exist."""
        client = AsyncMock()
        client.create_relation = AsyncMock()

        await _ensure_relation(client, "doc-1", "viewer", "user-1")

        client.create_relation.assert_called_once_with("document", "doc-1", "viewer", "user-1")

    @pytest.mark.anyio
    async def test_tolerates_duplicate_400(self):
        """Silently skips when Descope returns 400 (duplicate relation)."""
        response = MagicMock()
        response.status_code = 400
        error = httpx.HTTPStatusError("Bad Request", request=MagicMock(), response=response)
        client = AsyncMock()
        client.create_relation = AsyncMock(side_effect=error)

        # Should not raise
        await _ensure_relation(client, "doc-1", "viewer", "user-1")

    @pytest.mark.anyio
    async def test_propagates_non_400_errors(self):
        """Raises non-400 errors (e.g., 500 server error)."""
        response = MagicMock()
        response.status_code = 500
        error = httpx.HTTPStatusError("Server Error", request=MagicMock(), response=response)
        client = AsyncMock()
        client.create_relation = AsyncMock(side_effect=error)

        with pytest.raises(httpx.HTTPStatusError):
            await _ensure_relation(client, "doc-1", "viewer", "user-1")


# ---------------------------------------------------------------------------
# _seed_fga_relations
# ---------------------------------------------------------------------------


class TestSeedFgaRelations:
    def _make_doc(self, title, doc_id, tenant_id="t1"):
        doc = MagicMock()
        doc.title = title
        doc.id = doc_id
        doc.tenant_id = tenant_id
        return doc

    def _make_user(self, user_id, tenant_id, roles):
        return {
            "userId": user_id,
            "userTenants": [
                {"tenantId": tenant_id, "roleNames": roles},
            ],
        }

    @pytest.mark.anyio
    async def test_public_roadmap_all_viewers(self):
        """public-roadmap: all non-owner users get viewer relation."""
        client = AsyncMock()
        client.create_relation = AsyncMock()

        docs = [
            self._make_doc("public-roadmap", "road-1"),
            self._make_doc("board-minutes", "board-1"),
            self._make_doc("team-project", "proj-1"),
        ]

        users = [
            self._make_user("owner-1", "t1", ["owner"]),
            self._make_user("user-2", "t1", ["viewer"]),
            self._make_user("user-3", "t1", ["viewer"]),
        ]

        await _seed_fga_relations(client, docs, users, "owner-1")

        # Check public-roadmap: owner relation for owner-1, viewer for user-2 and user-3
        calls = client.create_relation.call_args_list
        roadmap_calls = [c for c in calls if c.args[1] == "road-1"]
        assert ("document", "road-1", "owner", "owner-1") in [c.args for c in roadmap_calls]
        viewer_targets = [c.args[3] for c in roadmap_calls if c.args[2] == "viewer"]
        assert "user-2" in viewer_targets
        assert "user-3" in viewer_targets

    @pytest.mark.anyio
    async def test_board_minutes_owners_only(self):
        """board-minutes: only owner/admin users get viewer access."""
        client = AsyncMock()
        client.create_relation = AsyncMock()

        docs = [
            self._make_doc("public-roadmap", "road-1"),
            self._make_doc("board-minutes", "board-1"),
            self._make_doc("team-project", "proj-1"),
        ]

        users = [
            self._make_user("owner-1", "t1", ["owner"]),
            self._make_user("admin-2", "t1", ["admin"]),
            self._make_user("user-3", "t1", ["viewer"]),
        ]

        await _seed_fga_relations(client, docs, users, "owner-1")

        calls = client.create_relation.call_args_list
        board_calls = [c for c in calls if c.args[1] == "board-1"]

        # owner-1 gets owner, admin-2 gets viewer
        board_targets = {c.args[3] for c in board_calls}
        assert "owner-1" in board_targets
        assert "admin-2" in board_targets
        # user-3 (not owner/admin) should NOT be in board-minutes
        assert "user-3" not in board_targets

    @pytest.mark.anyio
    async def test_team_project_editor_and_viewers(self):
        """team-project: first non-owner gets editor, rest get viewer."""
        client = AsyncMock()
        client.create_relation = AsyncMock()

        docs = [
            self._make_doc("public-roadmap", "road-1"),
            self._make_doc("board-minutes", "board-1"),
            self._make_doc("team-project", "proj-1"),
        ]

        users = [
            self._make_user("owner-1", "t1", ["owner"]),
            self._make_user("user-2", "t1", ["viewer"]),
            self._make_user("user-3", "t1", ["viewer"]),
            self._make_user("user-4", "t1", ["editor"]),
        ]

        await _seed_fga_relations(client, docs, users, "owner-1")

        calls = client.create_relation.call_args_list
        proj_calls = [c for c in calls if c.args[1] == "proj-1"]

        # owner-1 gets owner relation
        assert ("document", "proj-1", "owner", "owner-1") in [c.args for c in proj_calls]

        # First non-owner gets editor
        non_owner_calls = [c for c in proj_calls if c.args[3] != "owner-1"]
        assert non_owner_calls[0].args[2] == "editor"

        # Remaining non-owners get viewer
        for call in non_owner_calls[1:]:
            assert call.args[2] == "viewer"

    @pytest.mark.anyio
    async def test_no_non_owners(self):
        """Handles tenant with only owner-role users."""
        client = AsyncMock()
        client.create_relation = AsyncMock()

        docs = [
            self._make_doc("public-roadmap", "road-1"),
            self._make_doc("board-minutes", "board-1"),
            self._make_doc("team-project", "proj-1"),
        ]

        users = [
            self._make_user("owner-1", "t1", ["owner"]),
        ]

        # Should not raise
        await _seed_fga_relations(client, docs, users, "owner-1")

        calls = client.create_relation.call_args_list
        proj_calls = [c for c in calls if c.args[1] == "proj-1"]
        # Only owner relation, no editor/viewer
        assert len(proj_calls) == 1
        assert proj_calls[0].args[2] == "owner"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    @pytest.mark.anyio
    @patch("scripts.seed_demo._seed_fga_relations", new_callable=AsyncMock)
    @patch("scripts.seed_demo._get_or_create_documents", new_callable=AsyncMock)
    @patch("scripts.seed_demo.DescopeManagementClient")
    @patch("scripts.seed_demo._require_env")
    async def test_main_orchestrates_correctly(self, mock_require_env, mock_client_cls, mock_get_docs, mock_seed_fga):
        """main() orchestrates env -> client -> users -> docs -> relations."""
        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
            "DEMO_TENANT_ID": "t1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.search_tenant_users.return_value = [
            {"userId": "u1", "userTenants": [{"tenantId": "t1", "roleNames": ["owner"]}]},
        ]

        mock_doc = MagicMock()
        mock_get_docs.return_value = [mock_doc]

        await main()

        mock_client.search_tenant_users.assert_called_once_with("t1")
        mock_get_docs.assert_called_once_with("t1", "u1")
        mock_seed_fga.assert_called_once()

    @pytest.mark.anyio
    @patch("scripts.seed_demo.DescopeManagementClient")
    @patch("scripts.seed_demo._require_env")
    async def test_main_exits_if_no_tenant_users(self, mock_require_env, mock_client_cls):
        """main() exits with code 1 when no users found in tenant."""
        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
            "DEMO_TENANT_ID": "t1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.search_tenant_users.return_value = []

        with pytest.raises(SystemExit) as exc_info:
            await main()
        assert exc_info.value.code == 1

    @pytest.mark.anyio
    @patch("scripts.seed_demo._seed_fga_relations", new_callable=AsyncMock)
    @patch("scripts.seed_demo._get_or_create_documents", new_callable=AsyncMock)
    @patch("scripts.seed_demo.DescopeManagementClient")
    @patch("scripts.seed_demo._require_env")
    async def test_main_selects_owner_as_creator(self, mock_require_env, mock_client_cls, mock_get_docs, mock_seed_fga):
        """main() selects the first owner-role user as document creator."""
        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
            "DEMO_TENANT_ID": "t1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.search_tenant_users.return_value = [
            {"userId": "viewer-1", "userTenants": [{"tenantId": "t1", "roleNames": ["viewer"]}]},
            {"userId": "owner-1", "userTenants": [{"tenantId": "t1", "roleNames": ["owner"]}]},
        ]

        mock_get_docs.return_value = [MagicMock()]

        await main()

        # Owner user should be selected as the creator, not the viewer
        mock_get_docs.assert_called_once_with("t1", "owner-1")

    @pytest.mark.anyio
    @patch("scripts.seed_demo._seed_fga_relations", new_callable=AsyncMock)
    @patch("scripts.seed_demo._get_or_create_documents", new_callable=AsyncMock)
    @patch("scripts.seed_demo.DescopeManagementClient")
    @patch("scripts.seed_demo._require_env")
    async def test_main_selects_admin_as_creator(self, mock_require_env, mock_client_cls, mock_get_docs, mock_seed_fga):
        """main() selects admin-role user as creator when no owner exists."""
        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
            "DEMO_TENANT_ID": "t1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.search_tenant_users.return_value = [
            {"userId": "viewer-1", "userTenants": [{"tenantId": "t1", "roleNames": ["viewer"]}]},
            {"userId": "admin-1", "userTenants": [{"tenantId": "t1", "roleNames": ["admin"]}]},
        ]

        mock_get_docs.return_value = [MagicMock()]

        await main()

        mock_get_docs.assert_called_once_with("t1", "admin-1")

    @pytest.mark.anyio
    @patch("scripts.seed_demo.DescopeManagementClient")
    @patch("scripts.seed_demo._require_env")
    async def test_main_exits_on_client_init_failure(self, mock_require_env, mock_client_cls):
        """main() exits with code 1 when client initialization fails."""
        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
            "DEMO_TENANT_ID": "t1",
        }[k]

        mock_client_cls.side_effect = RuntimeError("init failed")

        with pytest.raises(SystemExit) as exc_info:
            await main()
        assert exc_info.value.code == 1

    @pytest.mark.anyio
    @patch("scripts.seed_demo.DescopeManagementClient")
    @patch("scripts.seed_demo._require_env")
    async def test_main_exits_on_search_users_failure(self, mock_require_env, mock_client_cls):
        """main() exits with code 1 when search_tenant_users fails."""
        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
            "DEMO_TENANT_ID": "t1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.search_tenant_users.side_effect = httpx.RequestError("connection failed", request=MagicMock())

        with pytest.raises(SystemExit) as exc_info:
            await main()
        assert exc_info.value.code == 1

    @pytest.mark.anyio
    @patch("scripts.seed_demo._seed_fga_relations", new_callable=AsyncMock)
    @patch("scripts.seed_demo._get_or_create_documents", new_callable=AsyncMock)
    @patch("scripts.seed_demo.DescopeManagementClient")
    @patch("scripts.seed_demo._require_env")
    async def test_main_falls_back_to_first_user(self, mock_require_env, mock_client_cls, mock_get_docs, mock_seed_fga):
        """main() falls back to first user as creator when no owner found."""
        mock_require_env.side_effect = lambda k: {
            "DESCOPE_PROJECT_ID": "proj-1",
            "DESCOPE_MANAGEMENT_KEY": "key-1",
            "DEMO_TENANT_ID": "t1",
        }[k]

        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.search_tenant_users.return_value = [
            {"userId": "viewer-1", "userTenants": [{"tenantId": "t1", "roleNames": ["viewer"]}]},
            {"userId": "viewer-2", "userTenants": [{"tenantId": "t1", "roleNames": ["viewer"]}]},
        ]

        mock_get_docs.return_value = [MagicMock()]

        await main()

        # Falls back to first user when no owner found
        mock_get_docs.assert_called_once_with("t1", "viewer-1")


class TestDemoDocumentsDefinition:
    def test_three_demo_documents_defined(self):
        """DEMO_DOCUMENTS constant has exactly three entries."""
        assert len(DEMO_DOCUMENTS) == 3

    def test_expected_titles(self):
        """DEMO_DOCUMENTS contains the expected document titles."""
        titles = {d["title"] for d in DEMO_DOCUMENTS}
        assert titles == {"public-roadmap", "board-minutes", "team-project"}

    def test_all_have_content(self):
        """All demo documents have non-empty content."""
        for doc in DEMO_DOCUMENTS:
            assert doc["content"], f"Document '{doc['title']}' has no content"
