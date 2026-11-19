"""The members of a tenant: what ``/api/members`` reads and changes, whichever provider holds them.

``MemberDirectory`` is what the members router is handed (``get_member_directory``).
A Descope deployment hands it the Descope management adapter, which keeps the
members in Descope under Descope's tenant and user ids. Every other deployment
hands it ``MemberService``: the canonical users and their memberships in Postgres,
each change pushed by the configured sync adapter (Keycloak: organization member).
"""

from __future__ import annotations

import uuid

from expression import Error, Ok, Result
from opentelemetry import trace

from app.errors.identity import Forbidden, IdentityError, NotFound
from app.services.adapters.base import PROVIDER_SYNC, PROVIDER_SYNC_FAILED
from app.services.descope_management import DescopeManagementAdapter
from app.services.role import RoleService
from app.services.user import MemberRefusal, MemberStatus, UserService

tracer = trace.get_tracer(__name__)


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _not_a_member(result: Result) -> Result:
    """A user the tenant does not have reads the same whether or not the user exists."""
    if result.is_error() and isinstance(result.error, NotFound):
        return Error(Forbidden(message=MemberRefusal.NOT_IN_TENANT))
    return result


class MemberService:
    """The tenant's canonical members; ``UserService`` and ``RoleService`` push each change."""

    def __init__(self, *, users: UserService, roles: RoleService) -> None:
        self._users = users
        self._roles = roles

    async def list_members(self, tenant_id: str) -> Result[dict, IdentityError]:
        """Every user holding a role in the tenant, with its roles there."""
        with tracer.start_as_current_span("MemberService.list_members") as span:
            span.set_attribute("tenant.id", tenant_id)
            tenant = _uuid(tenant_id)
            if tenant is None:
                return Ok({"members": []})
            listed = await self._users.list_members(tenant_id=tenant)
            return listed.map(lambda members: {"members": members})

    async def invite_member(self, *, email: str, tenant_id: str, role_names: list[str]) -> Result[dict, IdentityError]:
        """Create the user with ``email`` as a member of the tenant with ``role_names``, then push both.

        The roles are checked first, so an unknown role creates nobody. The user
        and its assignments commit together or not at all. An account with that
        email already exists is ``Conflict``, whichever tenant holds it, and is
        neither enrolled nor described.
        """
        with tracer.start_as_current_span("MemberService.invite_member") as span:
            span.set_attribute("tenant.id", tenant_id)
            tenant = _uuid(tenant_id)
            if tenant is None:
                return Error(Forbidden(message=MemberRefusal.NOT_IN_TENANT))
            role_ids = await self._roles.assignable_roles(tenant_id=tenant, role_names=role_names)
            if role_ids.is_error():
                return Error(role_ids.error)
            created = await self._users.create_member(
                tenant_id=tenant,
                email=email,
                stage_memberships=lambda user_id: self._roles.stage_member_roles(
                    user_id=user_id, tenant_id=tenant, role_ids=role_ids.ok
                ),
            )
            if created.is_error():
                return Error(created.error)
            member, assigned = created.ok
            roles_pushed = await self._roles.push_member_roles(assigned)
            user_pushed = member.pop(PROVIDER_SYNC, None) is None
            invited = {
                "status": "invited",
                "email": member["email"],
                "user": {**member, "roleNames": list(dict.fromkeys(role_names))},
            }
            # Committed either way; the provider does not hold all of it yet (as deactivate tells, #677).
            if not (user_pushed and roles_pushed.is_ok() and roles_pushed.ok):
                invited[PROVIDER_SYNC] = PROVIDER_SYNC_FAILED
            return Ok(invited)

    async def set_member_status(
        self, user_id: str, tenant_id: str, status: MemberStatus
    ) -> Result[dict | None, IdentityError]:
        """Enable or disable a member whose only tenant this is (``UserService.set_member_status``)."""
        with tracer.start_as_current_span("MemberService.set_member_status") as span:
            span.set_attribute("tenant.id", tenant_id)
            user, tenant = _uuid(user_id), _uuid(tenant_id)
            if user is None or tenant is None:
                return Error(Forbidden(message=MemberRefusal.NOT_IN_TENANT))
            return _not_a_member(await self._users.set_member_status(tenant_id=tenant, user_id=user, status=status))

    async def remove_member(self, user_id: str, tenant_id: str) -> Result[dict | None, IdentityError]:
        """End the user's membership of the tenant: every role it holds there (Keycloak: organization member)."""
        with tracer.start_as_current_span("MemberService.remove_member") as span:
            span.set_attribute("tenant.id", tenant_id)
            user, tenant = _uuid(user_id), _uuid(tenant_id)
            if user is None or tenant is None:
                return Error(Forbidden(message=MemberRefusal.NOT_IN_TENANT))
            return _not_a_member(await self._users.remove_user_from_tenant(tenant_id=tenant, user_id=user))


# What the members router is handed: the Descope adapter (Descope's ids) or the
# canonical members. Both take a tenant id and a user id as strings in the
# deployment's own spelling and answer a ``Result``.
MemberDirectory = DescopeManagementAdapter | MemberService
