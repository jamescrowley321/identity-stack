"""ZitadelSyncAdapter — pushes canonical tenants and users to Zitadel.

Outbound only, with a machine user's token (``ZitadelAdminApi``):

- ``sync_tenant`` creates the tenant's organization, gives it a login policy with
  self-registration off, grants it the project with every project role, and then links
  the tenant to it (``tenants.external_org_id`` and ``external_org_provider_id``, the
  ``zitadel`` provider row). The link is written last: an organization that failed part
  way is named by no tenant, and a first login into it provisions nobody.
- ``sync_user`` creates a human user in the tenant's organization when the user was just
  created there (``CREATED_IN_TENANT``), with the canonical user's id as its Zitadel id,
  and records it as the user's ``zitadel`` IdP link, so that user's first login finds
  them. For a linked user it brings the Zitadel user's state in line with the canonical
  status: deactivated when inactive, reactivated when active or provisioned.
- ``delete_user`` removes the linked Zitadel user.
- Roles, permissions and role assignments stay canonical: those methods, and
  ``delete_tenant``, return ``Ok`` and call nothing, as in ``OrySyncAdapter``.

Every method returns ``Error(SyncError)`` for any failure, Zitadel unreachable included,
and raises nothing. A user that was never in Zitadel through this adapter or a first
login has no link, so a status change or delete for it is an error, not a silent pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from expression import Error, Ok, Result
from opentelemetry import trace

from app.logbook import logbook
from app.outbound import UpstreamStatusError
from app.services.adapters.base import CREATED_IN_TENANT, IdentityProviderAdapter, SyncError
from app.services.adapters.zitadel_api import ZitadelAdminApi, login_policy_without_registration

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)

# The canonical ``providers`` row of Zitadel (scripts/seed_zitadel.py).
PROVIDER_NAME = "zitadel"

ACTIVE = "USER_STATE_ACTIVE"
INACTIVE = "USER_STATE_INACTIVE"
# Canonical status -> the Zitadel state the user is brought to.
_STATE_OF_STATUS = {"active": ACTIVE, "provisioned": ACTIVE, "inactive": INACTIVE}
# An upstream body in a SyncError message is cut to this many characters (conventions §9).
_BODY_LIMIT = 500


class ZitadelLinks(Protocol):
    """The canonical side: the ``zitadel`` provider row and what is linked to it."""

    async def provider_id(self) -> uuid.UUID | None: ...

    async def organization_of(self, tenant_id: uuid.UUID, provider_id: uuid.UUID) -> str | None: ...

    async def link_organization(self, tenant_id: uuid.UUID, provider_id: uuid.UUID, org_id: str) -> None: ...

    async def subject_of(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> str | None: ...

    async def link_subject(self, user_id: uuid.UUID, provider_id: uuid.UUID, sub: str, email: str) -> None: ...


ApiOpener = Callable[[], AbstractAsyncContextManager[ZitadelAdminApi]]


class _Refused(Exception):
    """A sync that stops before or between calls, with what its SyncError says."""

    def __init__(self, message: str, context: dict) -> None:
        super().__init__(message)
        self.message = message
        self.context = context


def _sync_error(operation: str, exc: Exception, context: dict) -> SyncError:
    """The SyncError for a failed sync: Zitadel's status and body, or the failure's type, never a credential."""
    if isinstance(exc, _Refused):
        return SyncError(message=exc.message, operation=operation, context=context | exc.context)
    if isinstance(exc, UpstreamStatusError):
        status = exc.response.status_code
        body = exc.response.text[:_BODY_LIMIT]
        return SyncError(
            message=f"Zitadel answered {status} to {operation}: {body}",
            operation=operation,
            context=context | {"status": status},
        )
    return SyncError(message=f"Zitadel {operation} failed: {type(exc).__name__}", operation=operation, context=context)


class ZitadelSyncAdapter(IdentityProviderAdapter):
    """Syncs canonical tenants and users to Zitadel. Roles and permissions stay canonical."""

    def __init__(self, open_api: ApiOpener, links: ZitadelLinks, *, project_id: str) -> None:
        self._open_api = open_api
        self._links = links
        self._project_id = project_id

    async def _provider_id(self) -> uuid.UUID:
        provider_id = await self._links.provider_id()
        if provider_id is None:
            raise _Refused(f"No {PROVIDER_NAME!r} provider row: run the Zitadel seed first", {})
        return provider_id

    async def sync_tenant(self, *, tenant_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create the tenant's organization, closed to self-registration and granted the project."""
        with tracer.start_as_current_span("zitadel.sync_tenant") as span:
            span.set_attribute("tenant.id", str(tenant_id))
            context: dict = {"tenant_id": str(tenant_id)}
            try:
                name = data.get("name")
                if not name:
                    raise _Refused("Missing required sync data: name is required", {})
                provider_id = await self._provider_id()
                if await self._links.organization_of(tenant_id, provider_id) is not None:
                    return Ok(None)
                async with self._open_api() as api:
                    org_id = await api.add_organization(name)
                    context["organization_id"] = org_id
                    policy = await api.effective_login_policy(org_id)
                    await api.add_login_policy(org_id, login_policy_without_registration(policy))
                    role_keys = await api.project_role_keys(self._project_id)
                    await api.grant_project(self._project_id, org_id, role_keys)
                await self._links.link_organization(tenant_id, provider_id, org_id)
                span.set_attribute("zitadel.organization_id", org_id)
                return Ok(None)
            except Exception as exc:
                return Error(_sync_error("sync_tenant", exc, context))

    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create the user in its tenant's organization when just created, then match the canonical status.

        Expected data keys: email, status; on creation also ``CREATED_IN_TENANT``,
        user_name, given_name and family_name. An empty given or family name is
        sent as the user name: Zitadel refuses an empty one.
        """
        with tracer.start_as_current_span("zitadel.sync_user") as span:
            span.set_attribute("user.id", str(user_id))
            context: dict = {"user_id": str(user_id)}
            try:
                wanted = _STATE_OF_STATUS.get(data.get("status") or "")
                if wanted is None:
                    raise _Refused(f"Unknown status: expected one of {sorted(_STATE_OF_STATUS)}", {})
                provider_id = await self._provider_id()
                sub = await self._links.subject_of(user_id, provider_id)
                created_in = data.get(CREATED_IN_TENANT)
                new_user: dict | None = None
                if sub is None:
                    if created_in is None:
                        raise _Refused("No Zitadel user is linked to this user", {})
                    new_user = await self._new_user(user_id, provider_id, uuid.UUID(str(created_in)), data)
                async with self._open_api() as api:
                    if new_user is not None:
                        await api.add_human_user(**new_user)
                        sub = new_user["user_id"]
                        await self._links.link_subject(user_id, provider_id, sub, new_user["email"])
                    state = await api.user_state(sub)
                    if state is None:
                        raise _Refused("The linked Zitadel user does not exist", {})
                    if wanted == INACTIVE and state == ACTIVE:
                        await api.deactivate_user(sub)
                    elif wanted == ACTIVE and state == INACTIVE:
                        await api.reactivate_user(sub)
                return Ok(None)
            except Exception as exc:
                return Error(_sync_error("sync_user", exc, context))

    async def _new_user(self, user_id: uuid.UUID, provider_id: uuid.UUID, tenant_id: uuid.UUID, data: dict) -> dict:
        """The human user to create in the tenant's organization, checked before Zitadel is called.

        Its Zitadel id is the canonical id, so the token ``sub`` of its logins is too.
        """
        email = data.get("email")
        user_name = data.get("user_name")
        if not email or not user_name:
            raise _Refused("Missing required sync data: email and user_name are required", {})
        org_id = await self._links.organization_of(tenant_id, provider_id)
        if org_id is None:
            raise _Refused("The tenant has no Zitadel organization", {"tenant_id": str(tenant_id)})
        return {
            "user_id": str(user_id),
            "org_id": org_id,
            "username": email,
            "given_name": data.get("given_name") or user_name,
            "family_name": data.get("family_name") or user_name,
            "email": email,
        }

    async def delete_user(self, *, user_id: uuid.UUID) -> Result[None, SyncError]:
        """Remove the linked Zitadel user. One Zitadel no longer has counts as removed."""
        with tracer.start_as_current_span("zitadel.delete_user") as span:
            span.set_attribute("user.id", str(user_id))
            context: dict = {"user_id": str(user_id)}
            try:
                provider_id = await self._provider_id()
                sub = await self._links.subject_of(user_id, provider_id)
                if sub is None:
                    raise _Refused("No Zitadel user is linked to this user", {})
                async with self._open_api() as api:
                    if not await api.delete_user(sub):
                        logger.info("Zitadel had no user %s to delete for canonical user %s", sub, user_id)
                return Ok(None)
            except Exception as exc:
                return Error(_sync_error("delete_user", exc, context))

    async def sync_role(self, *, role_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        # Roles are canonical-only.
        return Ok(None)

    async def sync_permission(self, *, permission_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        # Permissions are canonical-only.
        return Ok(None)

    async def sync_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]:
        # Role assignments are canonical-only.
        return Ok(None)

    async def delete_role_assignment(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        role_id: uuid.UUID,
        data: dict | None = None,
    ) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_role(self, *, role_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_permission(self, *, permission_id: uuid.UUID) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        # Out of scope (#569): nothing deletes a canonical tenant, and its organization stays.
        return Ok(None)
