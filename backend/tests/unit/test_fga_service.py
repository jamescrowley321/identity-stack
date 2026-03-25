"""Unit tests for the Descope FGA (Fine-Grained Authorization) service client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.fga import DescopeFGAClient, get_fga_client


@pytest.fixture
def client():
    return DescopeFGAClient("proj-123", "mgmt-key-456", "https://api.descope.com")


class TestDescopeFGAClient:
    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_create_relation(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.create_relation("document", "doc-1", "owner", "user-1")
        call_json = mock_http.request.call_args[1]["json"]
        assert call_json["relations"][0]["resource"] == "doc-1"
        assert call_json["relations"][0]["resourceType"] == "document"
        assert call_json["relations"][0]["relation"] == "owner"
        assert call_json["relations"][0]["target"] == "user-1"

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_delete_relation(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

        await client.delete_relation("document", "doc-1", "editor", "user-2")
        call_json = mock_http.request.call_args[1]["json"]
        assert call_json["relations"][0]["relation"] == "editor"
        assert call_json["relations"][0]["target"] == "user-2"

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_check_permission_allowed(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"allowed": True}),
        )

        result = await client.check_permission("document", "doc-1", "can_view", "user-1")
        assert result is True

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_check_permission_denied(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"allowed": False}),
        )

        result = await client.check_permission("document", "doc-1", "can_edit", "user-2")
        assert result is False

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_relations(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"relations": [{"target": "user-1", "relation": "owner"}]}),
        )

        result = await client.list_relations("document", "doc-1")
        assert len(result) == 1
        assert result[0]["target"] == "user-1"

    @pytest.mark.anyio
    @patch("app.services.descope.httpx.AsyncClient")
    async def test_list_user_resources(self, mock_cls, client):
        mock_http = AsyncMock()
        mock_cls.return_value.__aenter__.return_value = mock_http
        mock_http.request.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"resources": ["doc-1", "doc-2"]}),
        )

        result = await client.list_user_resources("document", "can_view", "user-1")
        assert result == ["doc-1", "doc-2"]


class TestGetFGAClient:
    def test_creates_client_from_env(self, monkeypatch):
        monkeypatch.setenv("DESCOPE_PROJECT_ID", "proj-env")
        monkeypatch.setenv("DESCOPE_MANAGEMENT_KEY", "key-env")
        client = get_fga_client()
        assert isinstance(client, DescopeFGAClient)
        assert client._auth_header == "Bearer proj-env:key-env"
