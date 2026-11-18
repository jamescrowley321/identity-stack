"""ZitadelSyncAdapter — pushes canonical tenants and users to Zitadel.

Outbound only, with a machine user's token (``ZitadelAdminApi``):

- ``sync_tenant`` creates the tenant's organization, with the tenant's id as its id,
  gives it a login policy with self-registration off, grants it the project with every
  project role, and then links the tenant to it (``tenants.external_org_id`` and
  ``external_org_provider_id``, the ``zitadel`` provider row). The link is written last:
  an organization that failed part way is named by no tenant, and a first login into it
  provisions nobody. A second sync of that tenant finishes it: the organization is
  found by its id (and must have the tenant's name), a policy already made is kept and a
  grant already made is set to the project's roles. An organization that already has
  users is not linked: the adapter creates users only after the link, so those got in
  some other way. A tenant linked to another provider's organization is refused before
  Zitadel is called.
- ``sync_user`` creates a human user in the tenant's organization when the user was just
  created there (``CREATED_IN_TENANT``) and has no link yet, with the canonical user's id
  as its Zitadel id, and records it as the user's ``zitadel`` IdP link, so that user's
  first login finds them; the new user is brought to the canonical status. Otherwise it
  pushes only a status change (``STATUS_CHANGED``, from activate and deactivate):
  deactivated unless already inactive, reactivated when inactive and wanted active or
  provisioned. The change reaches only a Zitadel user of the organization of the tenant
  it was made in (``CHANGED_IN_TENANT``): a tenant admin may give a user of another
  tenant a role, but does not decide whether that user's account in the other tenant's
  organization is active. A profile update or a tenant left sends no status change and reaches
  nothing, so it does not undo an enable or disable made in Zitadel, and memberships
  stay canonical.
- ``delete_user`` removes the linked Zitadel user.
- Roles, permissions and role assignments stay canonical: those methods, and
  ``delete_tenant``, return ``Ok`` and call nothing, as in ``OrySyncAdapter``.

Which Zitadel user: the adapter's token may change users of every organization, so it
follows only a link a sign-in or the backend made (``ZitadelLinks.subject_of``), never
one an admin typed in; a user whose Zitadel link is of another origin is refused before
Zitadel is called. A user with no link is looked up by its canonical id, the id the
adapter gives the users it creates: a create whose answer was lost, or whose link write
failed, is found and linked by the next call instead of being made again. Zitadel lets
any user manager choose a new user's id, so the user found by id is acted on only when
it is in the organization of one of the user's tenants, and linked only by the create
path, in the organization of the tenant the user was created in. A status change acts
only in the organization of the tenant it was made in, linked user or not.

Every method returns ``Error(SyncError)`` for any failure, Zitadel unreachable included,
and raises nothing; ``context[UNANSWERED]`` is set when Zitadel gave no readable answer
(no response, a timeout, a body that is not JSON). A user Zitadel has neither through a
followed link nor under its canonical id gets an error for a status change or delete,
not a silent pass. The adapter keeps no membership beside the canonical role
assignments (``ends_memberships`` is false): a user stays in the organization it was
created in, and the canonical model decides what it is granted.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from expression import Error, Ok, Result
from opentelemetry import trace

from app.logbook import logbook
from app.outbound import UpstreamStatusError, UpstreamTransportError
from app.services.adapters.base import (
    CHANGED_IN_TENANT,
    CREATED_IN_TENANT,
    STATUS_CHANGED,
    UNANSWERED,
    IdentityProviderAdapter,
    SyncError,
)
from app.services.adapters.zitadel_api import ZitadelAdminApi, ZitadelUser, login_policy_without_registration

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

    async def linked_elsewhere(self, tenant_id: uuid.UUID, provider_id: uuid.UUID) -> bool:
        """Whether the tenant is linked to an organization that is not one of this provider."""
        ...

    async def link_organization(self, tenant_id: uuid.UUID, provider_id: uuid.UUID, org_id: str) -> None: ...

    async def subject_of(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> str | None:
        """The Zitadel subject of the user's link, if a sign-in or the backend made it; never an admin-made one."""
        ...

    async def link_subject(self, user_id: uuid.UUID, provider_id: uuid.UUID, sub: str, email: str) -> None: ...

    async def has_link(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> bool:
        """Whether the user has a Zitadel link of any origin (a user has at most one per provider)."""
        ...

    async def organizations_of_user(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> frozenset[str]:
        """The Zitadel organizations of the tenants the user holds a role in."""
        ...


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
    if isinstance(exc, (UpstreamTransportError, json.JSONDecodeError)):
        # No response, a timeout, or a body that is not JSON: Zitadel gave no readable answer.
        context = context | {UNANSWERED: True}
    return SyncError(message=f"Zitadel {operation} failed: {type(exc).__name__}", operation=operation, context=context)


# A create Zitadel refused or did not answer: it may still have made what was asked
# (an earlier try made it, or the answer was lost), which a lookup by id settles.
_CREATE_FAILURES = (UpstreamStatusError, UpstreamTransportError, json.JSONDecodeError)
# Zitadel's answer to a create whose unique name (an organization's, a user's) is taken.
_TAKEN = 409


def _is_taken(exc: Exception) -> bool:
    return isinstance(exc, UpstreamStatusError) and exc.response.status_code == _TAKEN


async def _ensure_organization(api: ZitadelAdminApi, org_id: str, name: str) -> None:
    """Create the organization with that id, unless Zitadel already has the one an earlier try made.

    That one has the tenant's name too: an organization with the tenant's id and another
    name is not the adapter's, and is refused. A 409 with no organization of that id is a
    name another organization holds (names are unique in the instance).
    """
    try:
        await api.add_organization(org_id, name)
    except _CREATE_FAILURES as exc:
        found = await api.organization_name(org_id)
        if found is None:
            if _is_taken(exc):
                raise _Refused(
                    f"Zitadel already has an organization named {name!r}: rename the tenant, or that organization",
                    {"status": _TAKEN},
                ) from exc
            raise
        if found != name:
            raise _Refused(
                f"Zitadel has an organization with the tenant's id named {found!r}, not {name!r}: "
                "the adapter did not make it",
                {},
            ) from exc


async def _close_registration(api: ZitadelAdminApi, org_id: str) -> None:
    """Give the organization its own login policy with self-registration off, if it has none yet.

    The policy copies the default's other values. A default that links identity providers
    is refused: those links are not part of the copy, so login through them would stop.
    An own policy already there (an earlier try made it) is kept when it is closed to
    registration, and refused when it is not.
    """
    policy = await api.effective_login_policy(org_id)
    if policy.get("isDefault") is not True:
        if policy.get("allowRegister") is True:
            raise _Refused("The organization has a login policy of its own that allows self-registration", {})
        return
    if policy.get("idps"):
        raise _Refused(
            "The default login policy links identity providers, which the organization's own policy would drop",
            {},
        )
    await api.add_login_policy(org_id, login_policy_without_registration(policy))


async def _create_user(api: ZitadelAdminApi, new_user: dict) -> ZitadelUser | None:
    """Create the human user; what Zitadel then has with its id, None if nothing.

    A 409 with no user of that id is a user name another user holds (user names are
    unique in the instance; the adapter's is the email).
    """
    try:
        await api.add_human_user(**new_user)
    except _CREATE_FAILURES as exc:
        found = await api.find_user(new_user["user_id"])
        if found is None:
            if _is_taken(exc):
                raise _Refused(
                    f"Zitadel already has a user named {new_user['username']!r}, the user's email",
                    {"status": _TAKEN},
                ) from exc
            raise
        return found
    return await api.find_user(new_user["user_id"])


async def _same_id_user(api: ZitadelAdminApi, user_id: uuid.UUID, organizations: frozenset[str]) -> ZitadelUser:
    """The Zitadel user with the canonical id, if it is in one of ``organizations``; refused otherwise."""
    found = await api.find_user(str(user_id))
    if found is None:
        raise _Refused("No Zitadel user is linked to this user", {})
    _check_organization(found, organizations)
    return found


def _check_organization(found: ZitadelUser, organizations: frozenset[str]) -> None:
    if found.organization_id not in organizations:
        raise _Refused(
            "A Zitadel user with this user's id is in an organization of none of the user's tenants",
            {"organization_id": found.organization_id},
        )


def _check_changed_in(found: ZitadelUser, org_id: str | None) -> None:
    """Refuse a status change for a Zitadel user outside the organization of the tenant it was made in.

    The status was changed by an admin of that tenant. A user of another tenant's
    organization may hold a role there too (a role needs no earlier membership), and
    that admin does not decide whether their account in the other organization is active.
    """
    if found.organization_id != org_id:
        raise _Refused(
            "The Zitadel user is not in the organization of the tenant the status was changed in",
            {"organization_id": found.organization_id},
        )


async def _bring_to(api: ZitadelAdminApi, sub: str, state: str, wanted: str) -> None:
    """Bring the Zitadel user from ``state`` to the ``wanted`` one.

    Deactivated from any state but inactive (locked and initial included), so a canonical
    deactivation never passes over a user Zitadel still lets sign in. Reactivated only
    from inactive: a locked or initial user stays as Zitadel has it.
    """
    if wanted == INACTIVE and state != INACTIVE:
        try:
            await api.deactivate_user(sub)
        except UpstreamStatusError as exc:
            status = exc.response.status_code
            raise _Refused(
                f"Zitadel answered {status} to deactivating a user in state {state}: {exc.response.text[:_BODY_LIMIT]}",
                {"status": status, "state": state},
            ) from exc
    elif wanted == ACTIVE and state == INACTIVE:
        await api.reactivate_user(sub)


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
        """Create the tenant's organization, closed to self-registration and granted the project.

        The organization's id is the tenant's, so a sync that stopped part way is finished by
        the next one: an organization, a policy of its own or a grant Zitadel already has is
        kept, not made again.
        """
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
                if await self._links.linked_elsewhere(tenant_id, provider_id):
                    raise _Refused("The tenant is already linked to an organization of another provider", {})
                org_id = str(tenant_id)
                context["organization_id"] = org_id
                async with self._open_api() as api:
                    await _ensure_organization(api, org_id, name)
                    await _close_registration(api, org_id)
                    role_keys = await api.project_role_keys(self._project_id)
                    await api.grant_project(self._project_id, org_id, role_keys)
                    if await api.organization_has_users(org_id):
                        # The adapter creates users only after the link: these got in some other
                        # way, perhaps by self-registration before the policy was closed, and
                        # the link would make each a tenant member at its first login.
                        raise _Refused("The organization already has users the adapter did not create", {})
                await self._links.link_organization(tenant_id, provider_id, org_id)
                span.set_attribute("zitadel.organization_id", org_id)
                return Ok(None)
            except Exception as exc:
                return Error(_sync_error("sync_tenant", exc, context))

    async def sync_user(self, *, user_id: uuid.UUID, data: dict) -> Result[None, SyncError]:
        """Create the user in its tenant's organization when just created; push a status change.

        Expected data keys: email, status; on creation also ``CREATED_IN_TENANT``,
        user_name, given_name and family_name; ``STATUS_CHANGED: True`` and
        ``CHANGED_IN_TENANT`` on activate and deactivate. An empty given or family name is
        sent as the user name: Zitadel refuses an empty one. Any other change reaches nothing.

        A user with no followed link is looked up by its canonical id, the id this adapter
        gives the users it creates. On create, one an earlier try made but did not link (its
        answer was lost, or the link write failed) is linked instead of being made again. On
        a status change it gets the status and is not linked: only the create path links.
        """
        with tracer.start_as_current_span("zitadel.sync_user") as span:
            span.set_attribute("user.id", str(user_id))
            context: dict = {"user_id": str(user_id)}
            try:
                status_changed = data.get(STATUS_CHANGED) is True
                created_in = data.get(CREATED_IN_TENANT)
                if not status_changed and created_in is None:
                    return Ok(None)
                wanted = _STATE_OF_STATUS.get(data.get("status") or "")
                if wanted is None:
                    raise _Refused(f"Unknown status: expected one of {sorted(_STATE_OF_STATUS)}", {})
                provider_id = await self._provider_id()
                sub = await self._links.subject_of(user_id, provider_id)
                if sub is not None and not status_changed:
                    # Already in Zitadel (linked before this call): nothing was created, no status changed.
                    return Ok(None)
                changed_in = await self._changed_in(provider_id, data) if status_changed else None
                if sub is not None:
                    async with self._open_api() as api:
                        found = await api.find_user(sub)
                        if found is None:
                            raise _Refused("The linked Zitadel user does not exist", {})
                        _check_changed_in(found, changed_in)
                        await _bring_to(api, sub, found.state, wanted)
                    return Ok(None)
                await self._refuse_unfollowed_link(user_id, provider_id)
                if created_in is not None:
                    new_user = await self._new_user(user_id, provider_id, uuid.UUID(str(created_in)), data)
                    async with self._open_api() as api:
                        found = await api.find_user(str(user_id)) or await _create_user(api, new_user)
                        if found is None:
                            raise _Refused("Zitadel has no user with this user's id after creating it", {})
                        _check_organization(found, frozenset({new_user["org_id"]}))
                        await _bring_to(api, str(user_id), found.state, wanted)
                    await self._links.link_subject(user_id, provider_id, str(user_id), data.get("email") or "")
                    return Ok(None)
                # A status change with no followed link reaches the user with the canonical id
                # but never links it: any user manager of the organization can choose that id.
                async with self._open_api() as api:
                    found = await api.find_user(str(user_id))
                    if found is None:
                        raise _Refused("No Zitadel user is linked to this user", {})
                    _check_changed_in(found, changed_in)
                    await _bring_to(api, str(user_id), found.state, wanted)
                return Ok(None)
            except Exception as exc:
                return Error(_sync_error("sync_user", exc, context))

    async def _changed_in(self, provider_id: uuid.UUID, data: dict) -> str:
        """The organization of the tenant a status change was made in, checked before Zitadel is called."""
        tenant_id = data.get(CHANGED_IN_TENANT)
        if tenant_id is None:
            raise _Refused("A status change must name the tenant it was made in", {})
        org_id = await self._links.organization_of(uuid.UUID(str(tenant_id)), provider_id)
        if org_id is None:
            raise _Refused("The tenant has no Zitadel organization", {"tenant_id": str(tenant_id)})
        return org_id

    async def _refuse_unfollowed_link(self, user_id: uuid.UUID, provider_id: uuid.UUID) -> None:
        """Refuse a user whose Zitadel link the adapter does not follow, before Zitadel is called.

        The adapter would act on a Zitadel user that link does not name, and its own link
        could not be written beside it (one link per user and provider).
        """
        if await self._links.has_link(user_id, provider_id):
            raise _Refused(
                "The user has a Zitadel link the adapter does not follow: neither a sign-in nor the backend made it",
                {},
            )

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
        """Remove the linked Zitadel user, or the one this adapter created with the canonical id.

        A linked user Zitadel no longer has counts as removed. One found by the canonical id
        is removed only when it is in the organization of one of the user's tenants.
        """
        with tracer.start_as_current_span("zitadel.delete_user") as span:
            span.set_attribute("user.id", str(user_id))
            context: dict = {"user_id": str(user_id)}
            try:
                provider_id = await self._provider_id()
                sub = await self._links.subject_of(user_id, provider_id)
                if sub is not None:
                    async with self._open_api() as api:
                        if not await api.delete_user(sub):
                            logger.info("Zitadel had no user %s to delete for canonical user %s", sub, user_id)
                    return Ok(None)
                await self._refuse_unfollowed_link(user_id, provider_id)
                organizations = await self._links.organizations_of_user(user_id, provider_id)
                async with self._open_api() as api:
                    await _same_id_user(api, user_id, organizations)
                    if not await api.delete_user(str(user_id)):
                        raise _Refused("No Zitadel user is linked to this user", {})
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

    async def delete_role(self, *, role_id: uuid.UUID, data: dict | None = None) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_permission(self, *, permission_id: uuid.UUID, data: dict | None = None) -> Result[None, SyncError]:
        return Ok(None)

    async def delete_tenant(self, *, tenant_id: uuid.UUID) -> Result[None, SyncError]:
        # Out of scope (#569): nothing deletes a canonical tenant, and its organization stays.
        return Ok(None)
