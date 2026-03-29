import os
from typing import Literal

import httpx

# Default timeout for Descope API calls (seconds)
_DEFAULT_TIMEOUT = 30.0


class DescopeManagementClient:
    """Client for Descope Management API tenant operations.

    Accepts an httpx.AsyncClient for connection reuse. The caller is responsible
    for closing the client (typically via FastAPI lifespan).
    """

    def __init__(
        self,
        project_id: str,
        management_key: str,
        base_url: str = "https://api.descope.com",
        http_client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url
        self._auth_header = f"Bearer {project_id}:{management_key}"
        self._http_client = http_client
        self._owns_client = http_client is None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._auth_header}

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        return httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)

    async def _request(self, path: str, body: dict) -> httpx.Response:
        """Send a POST request to the Descope Management API."""
        client = self._get_client()
        try:
            resp = await client.post(
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp
        finally:
            if self._owns_client or self._http_client is None:
                await client.aclose()

    async def create_tenant(
        self,
        name: str,
        self_provisioning_domains: list[str] | None = None,
    ) -> dict:
        """Create a new tenant in Descope. Returns the tenant object with its ID."""
        body: dict = {"name": name}
        if self_provisioning_domains:
            body["selfProvisioningDomains"] = self_provisioning_domains
        resp = await self._request("/v1/mgmt/tenant/create", body)
        return resp.json()

    async def list_tenants(self) -> list[dict]:
        """List all tenants in the Descope project."""
        resp = await self._request("/v1/mgmt/tenant/all", {})
        return resp.json().get("tenants", [])

    async def load_tenant(self, tenant_id: str) -> dict:
        """Load a single tenant by ID."""
        resp = await self._request("/v1/mgmt/tenant/load", {"id": tenant_id})
        return resp.json()

    async def delete_tenant(self, tenant_id: str) -> None:
        """Delete a tenant by ID."""
        await self._request("/v1/mgmt/tenant/delete", {"id": tenant_id})

    async def add_user_to_tenant(self, user_id: str, tenant_id: str) -> None:
        """Add a user to a tenant."""
        await self._request(
            "/v1/mgmt/user/update/tenant/add",
            {"loginId": user_id, "tenantId": tenant_id},
        )

    async def assign_roles(self, user_id: str, tenant_id: str, role_names: list[str]) -> None:
        """Assign roles to a user within a specific tenant."""
        await self._request(
            "/v1/mgmt/user/update/role/add",
            {"loginId": user_id, "tenantId": tenant_id, "roleNames": role_names},
        )

    async def remove_roles(self, user_id: str, tenant_id: str, role_names: list[str]) -> None:
        """Remove roles from a user within a specific tenant."""
        await self._request(
            "/v1/mgmt/user/update/role/remove",
            {"loginId": user_id, "tenantId": tenant_id, "roleNames": role_names},
        )

    async def load_user(self, user_id: str) -> dict:
        """Load a user by login ID. Returns user object including customAttributes."""
        resp = await self._request("/v1/mgmt/user/load", {"loginId": user_id})
        return resp.json().get("user", {})

    async def update_user_custom_attribute(
        self, user_id: str, attribute_key: str, attribute_value: str | int | bool | float | None
    ) -> None:
        """Set a single custom attribute on a user."""
        await self._request(
            "/v1/mgmt/user/update/customAttribute",
            {"loginId": user_id, "attributeKey": attribute_key, "attributeValue": attribute_value},
        )

    async def update_tenant_custom_attributes(self, tenant_id: str, custom_attributes: dict) -> None:
        """Update custom attributes on a tenant."""
        await self._request(
            "/v1/mgmt/tenant/update",
            {"id": tenant_id, "customAttributes": custom_attributes},
        )

    async def create_access_key(
        self,
        name: str,
        tenant_id: str,
        expire_time: int | None = None,
        role_names: list[str] | None = None,
    ) -> dict:
        """Create an access key scoped to a tenant."""
        body: dict = {"name": name, "tenantId": tenant_id}
        if expire_time is not None:
            body["expireTime"] = expire_time
        if role_names:
            body["roleNames"] = role_names
        resp = await self._request("/v1/mgmt/accesskey/create", body)
        return resp.json()

    async def search_access_keys(self, tenant_id: str) -> list[dict]:
        """List access keys filtered by tenant."""
        resp = await self._request("/v1/mgmt/accesskey/search", {"tenantIds": [tenant_id]})
        return resp.json().get("keys", [])

    async def load_access_key(self, key_id: str) -> dict:
        """Load a single access key by ID."""
        resp = await self._request("/v1/mgmt/accesskey/load", {"id": key_id})
        return resp.json().get("key", {})

    async def deactivate_access_key(self, key_id: str) -> None:
        """Deactivate (revoke) an access key."""
        await self._request("/v1/mgmt/accesskey/deactivate", {"id": key_id})

    async def activate_access_key(self, key_id: str) -> None:
        """Reactivate a previously deactivated access key."""
        await self._request("/v1/mgmt/accesskey/activate", {"id": key_id})

    async def delete_access_key(self, key_id: str) -> None:
        """Permanently delete an access key."""
        await self._request("/v1/mgmt/accesskey/delete", {"id": key_id})

    async def list_permissions(self) -> list[dict]:
        """List all permission definitions in the Descope project."""
        resp = await self._request("/v1/mgmt/permission/all", {})
        return resp.json().get("permissions", [])

    async def create_permission(self, name: str, description: str = "") -> None:
        """Create a new permission definition."""
        await self._request("/v1/mgmt/permission/create", {"name": name, "description": description})

    async def update_permission(self, name: str, new_name: str, description: str = "") -> None:
        """Update an existing permission definition."""
        await self._request(
            "/v1/mgmt/permission/update", {"name": name, "newName": new_name, "description": description}
        )

    async def delete_permission(self, name: str) -> None:
        """Delete a permission definition by name."""
        await self._request("/v1/mgmt/permission/delete", {"name": name})

    async def list_roles(self) -> list[dict]:
        """List all role definitions in the Descope project."""
        resp = await self._request("/v1/mgmt/role/all", {})
        return resp.json().get("roles", [])

    async def create_role(self, name: str, description: str = "", permission_names: list[str] | None = None) -> None:
        """Create a new role definition with optional permission mappings."""
        body: dict = {"name": name, "description": description}
        if permission_names:
            body["permissionNames"] = permission_names
        await self._request("/v1/mgmt/role/create", body)

    async def update_role(
        self, name: str, new_name: str, description: str = "", permission_names: list[str] | None = None
    ) -> None:
        """Update an existing role definition."""
        body: dict = {"name": name, "newName": new_name, "description": description}
        if permission_names is not None:
            body["permissionNames"] = permission_names
        await self._request("/v1/mgmt/role/update", body)

    async def delete_role(self, name: str) -> None:
        """Delete a role definition by name."""
        await self._request("/v1/mgmt/role/delete", {"name": name})

    async def invite_user(self, email: str, tenant_id: str, role_names: list[str] | None = None) -> dict:
        """Create a user and assign them to a tenant with roles."""
        tenants = [{"tenantId": tenant_id}]
        if role_names:
            tenants[0]["roleNames"] = role_names
        resp = await self._request(
            "/v1/mgmt/user/create",
            {"loginId": email, "email": email, "tenants": tenants},
        )
        return resp.json().get("user", {})

    async def search_tenant_users(self, tenant_id: str) -> list[dict]:
        """Search for users belonging to a specific tenant."""
        resp = await self._request("/v1/mgmt/user/search", {"tenantIds": [tenant_id]})
        return resp.json().get("users", [])

    async def update_user_status(self, user_id: str, status: Literal["enabled", "disabled"]) -> None:
        """Update user status."""
        await self._request("/v1/mgmt/user/updateStatus", {"loginId": user_id, "status": status})

    async def remove_user_from_tenant(self, user_id: str, tenant_id: str) -> None:
        """Remove a user from a specific tenant (does not delete the user globally)."""
        await self._request(
            "/v1/mgmt/user/update/tenant/remove",
            {"loginId": user_id, "tenantId": tenant_id},
        )

    async def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._http_client is not None and not self._owns_client:
            await self._http_client.aclose()


# Module-level singleton for shared client
_descope_client: DescopeManagementClient | None = None


def get_descope_client() -> DescopeManagementClient:
    """Return the module-level DescopeManagementClient singleton.

    Must be initialized via init_descope_client() during app lifespan.
    Falls back to creating a per-call client if not initialized (e.g. in tests).
    """
    if _descope_client is not None:
        return _descope_client
    # Fallback for tests or scripts — no shared HTTP client
    project_id = os.environ["DESCOPE_PROJECT_ID"]
    management_key = os.environ["DESCOPE_MANAGEMENT_KEY"]
    base_url = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")
    return DescopeManagementClient(project_id, management_key, base_url)


def init_descope_client(http_client: httpx.AsyncClient | None = None) -> DescopeManagementClient:
    """Initialize the module-level singleton. Called from app lifespan."""
    global _descope_client
    project_id = os.environ["DESCOPE_PROJECT_ID"]
    management_key = os.environ["DESCOPE_MANAGEMENT_KEY"]
    base_url = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")
    _descope_client = DescopeManagementClient(project_id, management_key, base_url, http_client=http_client)
    return _descope_client


def shutdown_descope_client() -> None:
    """Clear the module-level singleton. Called from app lifespan."""
    global _descope_client
    _descope_client = None
