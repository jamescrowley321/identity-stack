import os

import httpx


class DescopeManagementClient:
    """Client for Descope Management API tenant operations."""

    def __init__(self, project_id: str, management_key: str, base_url: str = "https://api.descope.com"):
        self.base_url = base_url
        self._auth_header = f"Bearer {project_id}:{management_key}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._auth_header}

    async def create_tenant(
        self,
        name: str,
        self_provisioning_domains: list[str] | None = None,
    ) -> dict:
        """Create a new tenant in Descope. Returns the tenant object with its ID."""
        body: dict = {"name": name}
        if self_provisioning_domains:
            body["selfProvisioningDomains"] = self_provisioning_domains
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/tenant/create",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def list_tenants(self) -> list[dict]:
        """List all tenants in the Descope project."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/tenant/all",
                headers=self._headers(),
                json={},
            )
            resp.raise_for_status()
            return resp.json().get("tenants", [])

    async def load_tenant(self, tenant_id: str) -> dict:
        """Load a single tenant by ID."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/tenant/load",
                headers=self._headers(),
                json={"id": tenant_id},
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_tenant(self, tenant_id: str) -> None:
        """Delete a tenant by ID."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/tenant/delete",
                headers=self._headers(),
                json={"id": tenant_id},
            )
            resp.raise_for_status()

    async def add_user_to_tenant(self, user_id: str, tenant_id: str) -> None:
        """Add a user to a tenant."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/update/tenant/add",
                headers=self._headers(),
                json={"loginId": user_id, "tenantId": tenant_id},
            )
            resp.raise_for_status()


def get_descope_client() -> DescopeManagementClient:
    """Factory that creates a DescopeManagementClient from environment variables."""
    project_id = os.environ["DESCOPE_PROJECT_ID"]
    management_key = os.getenv("DESCOPE_MANAGEMENT_KEY", "")
    base_url = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")
    return DescopeManagementClient(project_id, management_key, base_url)
