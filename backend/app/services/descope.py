import os
from typing import Literal

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

    async def assign_roles(self, user_id: str, tenant_id: str, role_names: list[str]) -> None:
        """Assign roles to a user within a specific tenant."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/update/role/add",
                headers=self._headers(),
                json={"loginId": user_id, "tenantId": tenant_id, "roleNames": role_names},
            )
            resp.raise_for_status()

    async def remove_roles(self, user_id: str, tenant_id: str, role_names: list[str]) -> None:
        """Remove roles from a user within a specific tenant."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/update/role/remove",
                headers=self._headers(),
                json={"loginId": user_id, "tenantId": tenant_id, "roleNames": role_names},
            )
            resp.raise_for_status()

    async def load_user(self, user_id: str) -> dict:
        """Load a user by login ID. Returns user object including customAttributes."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/load",
                headers=self._headers(),
                json={"loginId": user_id},
            )
            resp.raise_for_status()
            return resp.json().get("user", {})

    async def update_user_custom_attribute(
        self, user_id: str, attribute_key: str, attribute_value: str | int | bool | float | None
    ) -> None:
        """Set a single custom attribute on a user."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/update/customAttribute",
                headers=self._headers(),
                json={"loginId": user_id, "attributeKey": attribute_key, "attributeValue": attribute_value},
            )
            resp.raise_for_status()

    async def update_tenant_custom_attributes(self, tenant_id: str, custom_attributes: dict) -> None:
        """Update custom attributes on a tenant."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/tenant/update",
                headers=self._headers(),
                json={"id": tenant_id, "customAttributes": custom_attributes},
            )
            resp.raise_for_status()

    async def create_access_key(
        self,
        name: str,
        tenant_id: str,
        expire_time: int | None = None,
        role_names: list[str] | None = None,
    ) -> dict:
        """Create an access key scoped to a tenant. Returns key object with cleartext (shown once)."""
        body: dict = {"name": name, "tenantId": tenant_id}
        if expire_time is not None:
            body["expireTime"] = expire_time
        if role_names:
            body["roleNames"] = role_names
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/accesskey/create",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def search_access_keys(self, tenant_id: str) -> list[dict]:
        """List access keys, optionally filtered by tenant."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/accesskey/search",
                headers=self._headers(),
                json={"tenantIds": [tenant_id]},
            )
            resp.raise_for_status()
            return resp.json().get("keys", [])

    async def load_access_key(self, key_id: str) -> dict:
        """Load a single access key by ID."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/accesskey/load",
                headers=self._headers(),
                json={"id": key_id},
            )
            resp.raise_for_status()
            return resp.json().get("key", {})

    async def deactivate_access_key(self, key_id: str) -> None:
        """Deactivate (revoke) an access key."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/accesskey/deactivate",
                headers=self._headers(),
                json={"id": key_id},
            )
            resp.raise_for_status()

    async def activate_access_key(self, key_id: str) -> None:
        """Reactivate a previously deactivated access key."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/accesskey/activate",
                headers=self._headers(),
                json={"id": key_id},
            )
            resp.raise_for_status()

    async def delete_access_key(self, key_id: str) -> None:
        """Permanently delete an access key."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/accesskey/delete",
                headers=self._headers(),
                json={"id": key_id},
            )
            resp.raise_for_status()

    async def invite_user(self, email: str, tenant_id: str, role_names: list[str] | None = None) -> dict:
        """Create a user and assign them to a tenant with roles."""
        tenants = [{"tenantId": tenant_id}]
        if role_names:
            tenants[0]["roleNames"] = role_names
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/create",
                headers=self._headers(),
                json={"loginId": email, "email": email, "tenants": tenants},
            )
            resp.raise_for_status()
            return resp.json().get("user", {})

    async def search_tenant_users(self, tenant_id: str) -> list[dict]:
        """Search for users belonging to a specific tenant."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/search",
                headers=self._headers(),
                json={"tenantIds": [tenant_id]},
            )
            resp.raise_for_status()
            return resp.json().get("users", [])

    async def update_user_status(self, user_id: str, status: Literal["enabled", "disabled"]) -> None:
        """Update user status."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/updateStatus",
                headers=self._headers(),
                json={"loginId": user_id, "status": status},
            )
            resp.raise_for_status()

    async def remove_user_from_tenant(self, user_id: str, tenant_id: str) -> None:
        """Remove a user from a specific tenant (does not delete the user globally)."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/mgmt/user/update/tenant/remove",
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
