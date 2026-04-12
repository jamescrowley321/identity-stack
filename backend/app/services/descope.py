import re
from typing import Literal

import httpx

# Default timeout for Descope API calls (seconds)
_DEFAULT_TIMEOUT = 30.0


class DescopeManagementClient:
    """Client for Descope Management API tenant operations.

    Accepts an httpx.AsyncClient for connection reuse. The caller is responsible
    for closing the client (typically via FastAPI lifespan).
    """

    _FGA_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_:.\-]+$")

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

    def _validate_fga_param(self, value: str, name: str) -> None:
        """Validate FGA identifier: non-empty, max 200 chars, safe characters only."""
        if not value or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        if len(value) > 200:
            raise ValueError(f"{name} must not exceed 200 characters")
        if not self._FGA_IDENTIFIER_PATTERN.match(value):
            raise ValueError(f"{name} contains invalid characters (allowed: alphanumeric, _, :, ., -)")

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

    async def _get(self, path: str) -> httpx.Response:
        """Send a GET request to the Descope Management API.

        Some Descope endpoints (notably /v1/mgmt/permission/all) only
        accept GET, unlike the majority which use POST.
        """
        client = self._get_client()
        try:
            resp = await client.get(
                f"{self.base_url}{path}",
                headers=self._headers(),
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
        resp = await self._request("/v1/mgmt/tenant/search", {})
        return resp.json().get("tenants", [])

    async def load_tenant(self, tenant_id: str) -> dict:
        """Load a single tenant by ID via search endpoint."""
        resp = await self._request("/v1/mgmt/tenant/search", {"tenantIds": [tenant_id]})
        tenants = resp.json().get("tenants", [])
        if not tenants:
            return {}
        return tenants[0]

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
        """Load a user by userId (from JWT sub claim). Returns user object including customAttributes.

        Uses the search endpoint with a userIds filter because the
        ``/v1/mgmt/user/load`` endpoint does not exist in the current
        Descope API version (returns 404).
        """
        resp = await self._request("/v1/mgmt/user/search", {"userIds": [user_id], "limit": 1})
        users = resp.json().get("users", [])
        return users[0] if users else {}

    async def resolve_login_id(self, user_id: str) -> str:
        """Resolve a userId (JWT sub) to the primary loginId required by mutation endpoints.

        Descope Management API mutation endpoints (role assignment, attribute updates,
        status changes) require loginId, not userId. This method loads the user and
        returns their primary loginId.

        Raises ValueError if the user has no loginIds.
        """
        user = await self.load_user(user_id)
        login_ids = user.get("loginIds", [])
        if not login_ids:
            raise ValueError(f"User {user_id} has no loginIds")
        return login_ids[0]

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
        """Create an access key scoped to a tenant.

        Uses ``keyTenants`` (not ``tenantId``) to associate the key with
        a tenant and its roles. Without this, the key is created but has
        no tenant associations and won't appear in tenant-scoped searches.
        """
        key_tenant: dict = {"tenantId": tenant_id}
        if role_names:
            key_tenant["roleNames"] = role_names
        body: dict = {"name": name, "keyTenants": [key_tenant]}
        if expire_time is not None:
            body["expireTime"] = expire_time
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
        """List all permission definitions in the Descope project.

        Unlike tenants and roles whose ``/search`` endpoint accepts POST,
        permissions only expose a ``/all`` endpoint that requires GET.
        """
        resp = await self._get("/v1/mgmt/permission/all")
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
        resp = await self._request("/v1/mgmt/role/search", {})
        return resp.json().get("roles") or []

    async def create_role(self, name: str, description: str = "", permission_names: list[str] | None = None) -> None:
        """Create a new role definition with optional permission mappings."""
        body: dict = {"name": name, "description": description}
        if permission_names is not None:
            body["permissionNames"] = permission_names
        await self._request("/v1/mgmt/role/create", body)

    async def update_role(
        self, name: str, new_name: str, description: str | None = None, permission_names: list[str] | None = None
    ) -> None:
        """Update an existing role definition."""
        body: dict = {"name": name, "newName": new_name}
        if description is not None:
            body["description"] = description
        if permission_names is not None:
            body["permissionNames"] = permission_names
        await self._request("/v1/mgmt/role/update", body)

    async def delete_role(self, name: str) -> None:
        """Delete a role definition by name."""
        await self._request("/v1/mgmt/role/delete", {"name": name})

    # --- FGA (Fine-Grained Authorization) methods ---

    async def get_fga_schema(self) -> dict:
        """Load the current FGA schema definition."""
        resp = await self._request("/v1/mgmt/authz/schema/load", {})
        return resp.json().get("schema", {})

    async def update_fga_schema(self, schema: str) -> None:
        """Save/update the FGA schema definition."""
        if not schema or not schema.strip():
            raise ValueError("schema must be a non-empty string")
        await self._request("/v1/mgmt/authz/schema/save", {"schema": schema})

    async def create_relation(self, resource_type: str, resource_id: str, relation: str, target: str) -> None:
        """Create an FGA relation tuple."""
        self._validate_fga_param(resource_type, "resource_type")
        self._validate_fga_param(resource_id, "resource_id")
        self._validate_fga_param(relation, "relation")
        self._validate_fga_param(target, "target")
        await self._request(
            "/v1/mgmt/authz/re/save",
            {
                "resourceType": resource_type,
                "resource": resource_id,
                "relationDefinition": relation,
                "target": target,
            },
        )

    async def delete_relation(self, resource_type: str, resource_id: str, relation: str, target: str) -> None:
        """Delete an FGA relation tuple."""
        self._validate_fga_param(resource_type, "resource_type")
        self._validate_fga_param(resource_id, "resource_id")
        self._validate_fga_param(relation, "relation")
        self._validate_fga_param(target, "target")
        await self._request(
            "/v1/mgmt/authz/re/delete",
            {
                "resourceType": resource_type,
                "resource": resource_id,
                "relationDefinition": relation,
                "target": target,
            },
        )

    async def list_relations(
        self, resource_type: str, resource_id: str, relation: str | None = None, target: str | None = None
    ) -> list[dict]:
        """List all relation tuples for a specific resource. Returns empty list if none."""
        self._validate_fga_param(resource_type, "resource_type")
        self._validate_fga_param(resource_id, "resource_id")
        if relation is not None:
            self._validate_fga_param(relation, "relation")
        if target is not None:
            self._validate_fga_param(target, "target")
        body: dict = {"resourceType": resource_type, "resource": resource_id}
        if relation is not None:
            body["relationDefinition"] = relation
        if target is not None:
            body["target"] = target
        resp = await self._request("/v1/mgmt/authz/re/who", body)
        return resp.json().get("relationInfo") or []

    async def list_user_resources(self, resource_type: str, relation: str, target: str) -> list[dict]:
        """List resources a target has a specific relation to. Returns empty list if none."""
        self._validate_fga_param(resource_type, "resource_type")
        self._validate_fga_param(relation, "relation")
        self._validate_fga_param(target, "target")
        resp = await self._request(
            "/v1/mgmt/authz/re/resource",
            {
                "resourceType": resource_type,
                "relationDefinition": relation,
                "target": target,
            },
        )
        return resp.json().get("resources") or []

    async def check_permission(self, resource_type: str, resource_id: str, relation: str, target: str) -> bool:
        """Check if a subject has a relation to a resource. Returns True/False.

        Raises httpx.HTTPStatusError on API errors (4xx/5xx).
        Raises httpx.RequestError on network/transport errors.
        Callers should handle these for fail-closed behavior.
        """
        self._validate_fga_param(resource_type, "resource_type")
        self._validate_fga_param(resource_id, "resource_id")
        self._validate_fga_param(relation, "relation")
        self._validate_fga_param(target, "target")
        resp = await self._request(
            "/v1/mgmt/authz/re/has",
            {
                "resourceType": resource_type,
                "resource": resource_id,
                "relationDefinition": relation,
                "target": target,
            },
        )
        return bool(resp.json().get("allowed", False))

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

    async def search_all_users(self, *, max_pages: int = 1000) -> list[dict]:
        """Search for all users in the Descope project, with pagination."""
        all_users: list[dict] = []
        limit = 100
        for page in range(max_pages):
            resp = await self._request(
                "/v1/mgmt/user/search",
                {"limit": limit, "page": page},
            )
            users = resp.json().get("users", [])
            all_users.extend(users)
            if len(users) < limit:
                break
        return all_users

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
