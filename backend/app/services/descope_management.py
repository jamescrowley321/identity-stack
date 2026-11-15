"""The Descope management boundary for the member, profile, tenant-settings and access-key routes.

Each method makes its calls through ``DescopeManagementClient`` and returns a
``Result`` carrying the API's own response shape, so no Descope payload or
upstream error type reaches a router.

An upstream failure a route answered before this adapter existed is an
``Error``. One it never caught still propagates, so the route still answers 500
(docs/conventions.md §4 names each one).
"""

from __future__ import annotations

from typing import Literal

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Forbidden, IdentityError, NotFound, ProviderError, ValidationError
from app.logbook import logbook
from app.outbound import UpstreamStatusError, UpstreamTransportError
from app.services.canonical import canonical_user
from app.services.descope import DescopeManagementClient

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

AttributeValue = str | int | bool | float | None


def _provider_error(operation: str, exc: UpstreamStatusError) -> ProviderError:
    return ProviderError(
        message=f"Descope API error {operation}",
        context={"status": exc.response.status_code},
    )


class DescopeManagementAdapter:
    """Result-returning Descope management operations, in the API's response shapes."""

    def __init__(self, client: DescopeManagementClient) -> None:
        self._client = client

    # --- Members ---

    async def list_members(self, tenant_id: str) -> Result[dict, IdentityError]:
        """Members of a tenant, each with the roles held in that tenant."""
        with tracer.start_as_current_span("DescopeManagementAdapter.list_members"):
            try:
                users = await self._client.search_tenant_users(tenant_id)
            except UpstreamStatusError as exc:
                logger.warning("Descope API error listing members: %s", exc.response.status_code)
                return Error(_provider_error("listing members", exc))
            members = []
            for user in users:
                tenant_roles = []
                for membership in user.get("userTenants", []):
                    if membership.get("tenantId") == tenant_id:
                        tenant_roles = membership.get("roleNames", [])
                        break
                members.append(
                    {
                        "userId": user.get("userId", ""),
                        "email": user.get("email", ""),
                        "name": user.get("name", ""),
                        "status": user.get("status", "enabled"),
                        "roleNames": tenant_roles,
                    }
                )
            return Ok({"members": members})

    async def invite_member(self, *, email: str, tenant_id: str, role_names: list[str]) -> Result[dict, IdentityError]:
        """Create a user in a tenant. A 400 from Descope is a ``ValidationError`` with its description."""
        with tracer.start_as_current_span("DescopeManagementAdapter.invite_member"):
            try:
                user = await self._client.invite_user(email=email, tenant_id=tenant_id, role_names=role_names)
            except UpstreamStatusError as exc:
                if exc.response.status_code == 400:
                    return Error(ValidationError(message=exc.response.json().get("errorDescription", "Bad request")))
                logger.warning("Descope API error inviting member: %s", exc.response.status_code)
                return Error(_provider_error("inviting member", exc))
            return Ok({"status": "invited", "email": email, "user": canonical_user(user)})

    async def set_member_status(
        self, user_id: str, tenant_id: str, status: Literal["enabled", "disabled"]
    ) -> Result[None, IdentityError]:
        """Enable or disable a member of ``tenant_id``.

        Descope's status call takes no tenant and applies across the project, so
        membership is checked first. A user outside the tenant is ``NotFound``,
        the same as one Descope does not have, and the status call is not made.
        """
        with tracer.start_as_current_span("DescopeManagementAdapter.set_member_status"):
            try:
                user = await self._client.load_user(user_id)
                memberships = user.get("userTenants") or []
                if tenant_id not in [m.get("tenantId") for m in memberships]:
                    logger.warning("Refused status change: %s is not a member of tenant %s", user_id, tenant_id)
                    return Error(NotFound(message="Member not found in tenant"))
                await self._client.update_user_status(user_id, status)
            except UpstreamStatusError as exc:
                action = "deactivating" if status == "disabled" else "activating"
                logger.warning("Descope API error %s member: %s", action, exc.response.status_code)
                return Error(_provider_error("setting member status", exc))
            return Ok(None)

    async def remove_member(self, user_id: str, tenant_id: str) -> Result[None, IdentityError]:
        """Remove a user from a tenant."""
        with tracer.start_as_current_span("DescopeManagementAdapter.remove_member"):
            try:
                await self._client.remove_user_from_tenant(user_id, tenant_id)
            except UpstreamStatusError as exc:
                logger.warning("Descope API error removing member: %s", exc.response.status_code)
                return Error(_provider_error("removing member", exc))
            return Ok(None)

    # --- Profile ---

    async def load_profile(self, user_id: str) -> Result[dict, IdentityError]:
        """A user's profile and custom attributes. Both upstream failures are an ``Error``."""
        with tracer.start_as_current_span("DescopeManagementAdapter.load_profile"):
            try:
                user = await self._client.load_user(user_id)
            except (UpstreamStatusError, UpstreamTransportError) as exc:
                logger.warning("Descope API error loading profile for %s: %s", user_id, exc)
                return Error(ProviderError(message="Descope API error loading profile"))
            return Ok(
                {
                    "user_id": user_id,
                    "name": user.get("name", ""),
                    "email": user.get("email", ""),
                    "custom_attributes": user.get("customAttributes", {}),
                }
            )

    async def update_profile_attribute(
        self, user_id: str, key: str, value: AttributeValue
    ) -> Result[None, IdentityError]:
        """Set one custom attribute, resolving the userId to the loginId Descope's mutation needs."""
        with tracer.start_as_current_span("DescopeManagementAdapter.update_profile_attribute"):
            try:
                login_id = await self._client.resolve_login_id(user_id)
                await self._client.update_user_custom_attribute(login_id, key, value)
            except UpstreamStatusError as exc:
                logger.warning(
                    "Descope API error updating attribute %s for %s: %s", key, user_id, exc.response.status_code
                )
                return Error(_provider_error("updating attribute", exc))
            return Ok(None)

    # --- Tenant settings ---

    async def load_tenant_settings(self, tenant_id: str) -> Result[dict, IdentityError]:
        """A tenant's name and custom attributes. ``NotFound`` when Descope has no such tenant."""
        with tracer.start_as_current_span("DescopeManagementAdapter.load_tenant_settings"):
            try:
                tenant = await self._client.load_tenant(tenant_id)
            except UpstreamStatusError as exc:
                if exc.response.status_code == 404:
                    return Error(NotFound(message="Tenant not found in Descope"))
                logger.warning("Descope API error loading tenant %s: %s", tenant_id, exc.response.status_code)
                return Error(_provider_error("loading tenant", exc))
            if not tenant:
                return Error(NotFound(message="Tenant not found in Descope"))
            return Ok(
                {
                    "tenant_id": tenant_id,
                    "name": tenant.get("name", ""),
                    "custom_attributes": tenant.get("customAttributes", {}),
                }
            )

    async def update_tenant_settings(
        self, tenant_id: str, custom_attributes: dict[str, AttributeValue]
    ) -> Result[None, IdentityError]:
        """Update a tenant's custom attributes."""
        with tracer.start_as_current_span("DescopeManagementAdapter.update_tenant_settings"):
            try:
                await self._client.update_tenant_custom_attributes(tenant_id, custom_attributes)
            except UpstreamStatusError as exc:
                logger.warning("Descope API error updating tenant %s: %s", tenant_id, exc.response.status_code)
                return Error(_provider_error("updating tenant", exc))
            return Ok(None)

    # --- Access keys ---
    # The API returns access keys as Descope reports them, so these shapes are the upstream ones.

    async def create_access_key(
        self, *, name: str, tenant_id: str, expire_time: int, role_names: list[str] | None
    ) -> Result[dict, IdentityError]:
        """Create a key scoped to a tenant. The value carries the cleartext, shown once."""
        with tracer.start_as_current_span("DescopeManagementAdapter.create_access_key"):
            return Ok(
                await self._client.create_access_key(
                    name=name, tenant_id=tenant_id, expire_time=expire_time, role_names=role_names
                )
            )

    async def list_access_keys(self, tenant_id: str) -> Result[dict, IdentityError]:
        """A tenant's access keys."""
        with tracer.start_as_current_span("DescopeManagementAdapter.list_access_keys"):
            return Ok({"keys": await self._client.search_access_keys(tenant_id)})

    async def load_tenant_access_key(self, key_id: str, tenant_id: str) -> Result[dict, IdentityError]:
        """Load a key. ``Forbidden`` unless it belongs to ``tenant_id``."""
        with tracer.start_as_current_span("DescopeManagementAdapter.load_tenant_access_key"):
            key = await self._client.load_access_key(key_id)
            key_tenants = [t.get("tenantId", "") for t in key.get("keyTenants") or []]
            if tenant_id not in key_tenants:
                return Error(Forbidden(message="Access key belongs to another tenant"))
            return Ok(key)

    async def deactivate_access_key(self, key_id: str) -> Result[None, IdentityError]:
        """Deactivate (revoke) a key."""
        with tracer.start_as_current_span("DescopeManagementAdapter.deactivate_access_key"):
            await self._client.deactivate_access_key(key_id)
            return Ok(None)

    async def activate_access_key(self, key_id: str) -> Result[None, IdentityError]:
        """Reactivate a deactivated key."""
        with tracer.start_as_current_span("DescopeManagementAdapter.activate_access_key"):
            await self._client.activate_access_key(key_id)
            return Ok(None)

    async def delete_access_key(self, key_id: str) -> Result[None, IdentityError]:
        """Permanently delete a key."""
        with tracer.start_as_current_span("DescopeManagementAdapter.delete_access_key"):
            await self._client.delete_access_key(key_id)
            return Ok(None)
