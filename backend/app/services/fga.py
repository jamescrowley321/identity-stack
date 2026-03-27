"""Descope FGA (Fine-Grained Authorization) service client.

Wraps the Descope AuthZ Management API for relationship-based access control.
Extends DescopeManagementClient to reuse retry logic with exponential backoff.
"""

import os

from app.services.descope import DescopeManagementClient


class DescopeFGAClient(DescopeManagementClient):
    """Client for Descope FGA (AuthZ) API — manages relations and permission checks."""

    async def create_relation(self, resource_type: str, resource_id: str, relation: str, user_id: str) -> None:
        """Create a relation tuple: user has relation to resource."""
        await self._request(
            "POST",
            "/v1/mgmt/authz/re/save",
            json={
                "relations": [
                    {
                        "resource": resource_id,
                        "resourceType": resource_type,
                        "relation": relation,
                        "target": user_id,
                        "targetSetResource": "",
                        "targetSetResourceType": "",
                        "targetSetRelation": "",
                    }
                ]
            },
        )

    async def delete_relation(self, resource_type: str, resource_id: str, relation: str, user_id: str) -> None:
        """Delete a relation tuple."""
        await self._request(
            "POST",
            "/v1/mgmt/authz/re/delete",
            json={
                "relations": [
                    {
                        "resource": resource_id,
                        "resourceType": resource_type,
                        "relation": relation,
                        "target": user_id,
                        "targetSetResource": "",
                        "targetSetResourceType": "",
                        "targetSetRelation": "",
                    }
                ]
            },
        )

    async def check_permission(self, resource_type: str, resource_id: str, relation: str, user_id: str) -> bool:
        """Check if user has the specified relation/permission on a resource."""
        resp = await self._request(
            "POST",
            "/v1/mgmt/authz/re/has",
            json={
                "resource": resource_id,
                "resourceType": resource_type,
                "relationDefinition": relation,
                "target": user_id,
            },
        )
        return resp.json().get("allowed", False)

    async def list_relations(self, resource_type: str, resource_id: str) -> list[dict]:
        """List all users with relations to a resource."""
        resp = await self._request(
            "POST",
            "/v1/mgmt/authz/re/who",
            json={"resource": resource_id, "resourceType": resource_type},
        )
        return resp.json().get("relations", [])

    async def list_user_resources(self, resource_type: str, relation: str, user_id: str) -> list[str]:
        """List resource IDs that a user has the specified relation with."""
        resp = await self._request(
            "POST",
            "/v1/mgmt/authz/re/resource",
            json={
                "resourceType": resource_type,
                "relationDefinition": relation,
                "target": user_id,
            },
        )
        return resp.json().get("resources", [])


_fga_client: DescopeFGAClient | None = None


def get_fga_client() -> DescopeFGAClient:
    """Return a cached DescopeFGAClient singleton (one per process)."""
    global _fga_client
    if _fga_client is None:
        project_id = os.environ["DESCOPE_PROJECT_ID"]
        management_key = os.getenv("DESCOPE_MANAGEMENT_KEY", "")
        base_url = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")
        _fga_client = DescopeFGAClient(project_id, management_key, base_url)
    return _fga_client
