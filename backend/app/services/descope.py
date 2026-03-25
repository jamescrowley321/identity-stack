import asyncio
import os
import random

import httpx

from app.logging_config import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
MAX_RETRIES = int(os.getenv("DESCOPE_MAX_RETRIES", "3"))
RETRY_BASE_DELAY = float(os.getenv("DESCOPE_RETRY_BASE_DELAY", "0.5"))
RETRY_MAX_DELAY = float(os.getenv("DESCOPE_RETRY_MAX_DELAY", "30"))


def _backoff_delay(attempt: int) -> float:
    """Calculate delay with full jitter: random(0, min(max_delay, base * 2^attempt))."""
    ceiling = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2**attempt))
    return random.uniform(0, ceiling)


class DescopeManagementClient:
    """Client for Descope Management API with automatic retry on transient errors.

    Retries on connection errors, timeouts, 429 (rate limit), and 502/503/504 (server errors).
    Non-retryable errors (400, 401, 403, 404) fail immediately.
    """

    def __init__(self, project_id: str, management_key: str, base_url: str = "https://api.descope.com"):
        self.base_url = base_url
        self._auth_header = f"Bearer {project_id}:{management_key}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._auth_header}

    async def _request(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        """Make an HTTP request with exponential backoff retry for transient errors."""
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.request(method, url, headers=self._headers(), json=json)
                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "descope.retry attempt=%d status=%d delay=%.2fs path=%s",
                        attempt + 1,
                        resp.status_code,
                        delay,
                        path,
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "descope.retry attempt=%d error=%s delay=%.2fs path=%s",
                        attempt + 1,
                        type(exc).__name__,
                        delay,
                        path,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    async def create_tenant(
        self,
        name: str,
        self_provisioning_domains: list[str] | None = None,
    ) -> dict:
        """Create a new tenant in Descope. Returns the tenant object with its ID."""
        body: dict = {"name": name}
        if self_provisioning_domains:
            body["selfProvisioningDomains"] = self_provisioning_domains
        resp = await self._request("POST", "/v1/mgmt/tenant/create", json=body)
        return resp.json()

    async def list_tenants(self) -> list[dict]:
        """List all tenants in the Descope project."""
        resp = await self._request("POST", "/v1/mgmt/tenant/all", json={})
        return resp.json().get("tenants", [])

    async def load_tenant(self, tenant_id: str) -> dict:
        """Load a single tenant by ID."""
        resp = await self._request("POST", "/v1/mgmt/tenant/load", json={"id": tenant_id})
        return resp.json()

    async def delete_tenant(self, tenant_id: str) -> None:
        """Delete a tenant by ID."""
        await self._request("POST", "/v1/mgmt/tenant/delete", json={"id": tenant_id})

    async def add_user_to_tenant(self, user_id: str, tenant_id: str) -> None:
        """Add a user to a tenant."""
        await self._request("POST", "/v1/mgmt/user/update/tenant/add", json={"loginId": user_id, "tenantId": tenant_id})

    async def assign_roles(self, user_id: str, tenant_id: str, role_names: list[str]) -> None:
        """Assign roles to a user within a specific tenant."""
        await self._request(
            "POST",
            "/v1/mgmt/user/update/role/add",
            json={"loginId": user_id, "tenantId": tenant_id, "roleNames": role_names},
        )

    async def remove_roles(self, user_id: str, tenant_id: str, role_names: list[str]) -> None:
        """Remove roles from a user within a specific tenant."""
        await self._request(
            "POST",
            "/v1/mgmt/user/update/role/remove",
            json={"loginId": user_id, "tenantId": tenant_id, "roleNames": role_names},
        )

    async def load_user(self, user_id: str) -> dict:
        """Load a user by login ID. Returns user object including customAttributes."""
        resp = await self._request("POST", "/v1/mgmt/user/load", json={"loginId": user_id})
        return resp.json().get("user", {})

    async def update_user_custom_attribute(
        self, user_id: str, attribute_key: str, attribute_value: str | int | bool | float | None
    ) -> None:
        """Set a single custom attribute on a user."""
        await self._request(
            "POST",
            "/v1/mgmt/user/update/customAttribute",
            json={"loginId": user_id, "attributeKey": attribute_key, "attributeValue": attribute_value},
        )

    async def update_tenant_custom_attributes(self, tenant_id: str, custom_attributes: dict) -> None:
        """Update custom attributes on a tenant."""
        await self._request(
            "POST",
            "/v1/mgmt/tenant/update",
            json={"id": tenant_id, "customAttributes": custom_attributes},
        )

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
        resp = await self._request("POST", "/v1/mgmt/accesskey/create", json=body)
        return resp.json()

    async def search_access_keys(self, tenant_id: str) -> list[dict]:
        """List access keys, optionally filtered by tenant."""
        resp = await self._request("POST", "/v1/mgmt/accesskey/search", json={"tenantIds": [tenant_id]})
        return resp.json().get("keys", [])

    async def load_access_key(self, key_id: str) -> dict:
        """Load a single access key by ID."""
        resp = await self._request("POST", "/v1/mgmt/accesskey/load", json={"id": key_id})
        return resp.json().get("key", {})

    async def deactivate_access_key(self, key_id: str) -> None:
        """Deactivate (revoke) an access key."""
        await self._request("POST", "/v1/mgmt/accesskey/deactivate", json={"id": key_id})

    async def activate_access_key(self, key_id: str) -> None:
        """Reactivate a previously deactivated access key."""
        await self._request("POST", "/v1/mgmt/accesskey/activate", json={"id": key_id})

    async def delete_access_key(self, key_id: str) -> None:
        """Permanently delete an access key."""
        await self._request("POST", "/v1/mgmt/accesskey/delete", json={"id": key_id})

    async def invite_user(self, email: str, tenant_id: str, role_names: list[str] | None = None) -> dict:
        """Create a user and assign them to a tenant with roles."""
        tenants = [{"tenantId": tenant_id}]
        if role_names:
            tenants[0]["roleNames"] = role_names
        resp = await self._request(
            "POST",
            "/v1/mgmt/user/create",
            json={"loginId": email, "email": email, "tenants": tenants},
        )
        return resp.json().get("user", {})

    async def search_tenant_users(self, tenant_id: str) -> list[dict]:
        """Search for users belonging to a specific tenant."""
        resp = await self._request("POST", "/v1/mgmt/user/search", json={"tenantIds": [tenant_id]})
        return resp.json().get("users", [])

    async def update_user_status(self, user_id: str, status: str) -> None:
        """Update user status. status must be 'enabled' or 'disabled'."""
        await self._request("POST", "/v1/mgmt/user/updateStatus", json={"loginId": user_id, "status": status})

    async def delete_user(self, user_id: str) -> None:
        """Delete a user permanently."""
        await self._request("POST", "/v1/mgmt/user/delete", json={"loginId": user_id})


def get_descope_client() -> DescopeManagementClient:
    """Factory that creates a DescopeManagementClient from environment variables."""
    project_id = os.environ["DESCOPE_PROJECT_ID"]
    management_key = os.getenv("DESCOPE_MANAGEMENT_KEY", "")
    base_url = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")
    return DescopeManagementClient(project_id, management_key, base_url)
