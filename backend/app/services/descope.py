import os

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
