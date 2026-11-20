"""Role and permission dependencies.

All of them take the tenant and the caller's grants from the one resolver,
``app.dependencies.tenant_access.resolve_tenant_access`` (conventions §18). This
module supplies the half of it that does I/O: loading a caller's canonical
memberships.

Two shapes of principal reach these dependencies:

* Providers that carry authorization **in the token** — Descope, via its ``dct``
  (current tenant) and ``tenants`` (memberships with roles/permissions) claims.
  These are decided from the claims alone, with no database read, exactly as
  before.
* Providers that carry **no** authorization claims — Ory, and standard OIDC
  generally. Their tokens say who the caller is and nothing about what the caller
  may do, so roles and permissions are resolved from the canonical model instead:
  the same ``(provider, sub)`` lookup that backs ``GET /api/identity``.

Epic 3 moved roles and tenants into the canonical model but left the authorization
path reading Descope claim shapes, which meant every role-gated route answered 403
"No tenant context" for an Ory principal. This closes that gap without changing
what a Descope token authorizes.

Which providers take the canonical path is configuration, not code:
``CANONICAL_RBAC_PROVIDERS`` (default ``ory``), mirroring ``JIT_ENABLED_PROVIDERS``
in ``app.routers.protected``. Descope is deliberately absent from the default set —
its authorization decisions still come only from its own signed claims.

Every branch of the canonical path that cannot produce an unambiguous answer fails
closed with 403. An authorization decision is never made on a guess.
"""

import enum
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.params import Depends as DependsMarker
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.tenant_access import (
    NO_TENANT_CONTEXT,
    CanonicalGrant,
    CanonicalMemberships,
    TenantAccess,
    resolve_tenant_access,
)
from app.domain import access as access_domain
from app.domain import credentials
from app.errors.identity import IdentityErrorRaised, MachinePrincipalRefused
from app.knobs import knob_set
from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig
from app.models.database import get_session_factory
from app.models.identity.tenant import TenantStatus
from app.models.identity.user import UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
from app.repositories.machine_credential import MachineCredentialRepository
from app.repositories.provider import ProviderRepository
from app.repositories.role import RoleRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.identity_resolution import IdentityResolutionService

logger = logbook(__name__)

# Providers whose tokens carry no authorization claims — their roles and
# permissions come from the canonical model. Descope is excluded by default:
# it signs its own `dct`/`tenants` claims and those stay authoritative.
CANONICAL_RBAC_PROVIDERS = knob_set("CANONICAL_RBAC_PROVIDERS", "ory")

INSUFFICIENT_PERMISSIONS = "Insufficient permissions"


async def get_rbac_session() -> AsyncIterator[AsyncSession | None]:
    """Yield a session for canonical role resolution, or None when there is no database.

    Separate from ``get_async_session`` on purpose. This dependency resolves on
    every role-gated route, including the access-key routes that talk only to the
    identity provider and have no database dependency of their own. Those must keep
    working wherever they work today, so a missing or malformed ``DATABASE_URL``
    yields None here and the canonical path fails closed with 403 rather than
    turning a 403 into a 500.

    Creating a session is lazy — SQLAlchemy acquires no connection until a
    statement runs — so a request decided from the token alone pays nothing for it.
    """
    try:
        factory = get_session_factory()
    except RuntimeError:
        logger.debug("no database configured; canonical role resolution unavailable")
        yield None
        return
    async with factory() as session:
        yield session


def _is_descope_principal(request: Request) -> bool:
    """Whether a middleware recorded a ``descope_managed`` provider for the request's claims."""
    provider = getattr(request.state, "provider", None)
    return isinstance(provider, ProviderTokenConfig) and provider.descope_managed


def _principal_provider(request: Request) -> str:
    """The name of the provider that authenticated this request, lowercased."""
    principal = getattr(request.state, "principal", None)
    identity = getattr(principal, "identity", None)
    authentication_type = getattr(identity, "authentication_type", None)
    return authentication_type.lower() if isinstance(authentication_type, str) else ""


def _machine_client(request: Request, claims: dict) -> str | None:
    """The machine credential client the token was issued to, by its provider's reader; ``None`` for a person."""
    provider = getattr(request.state, "provider", None)
    if not isinstance(provider, ProviderTokenConfig) or provider.machine_client is None:
        return None
    return provider.machine_client(claims)


MACHINE_PRINCIPAL_REFUSED = "A machine credential cannot manage credentials, roles or members"
MACHINE_ROLES_REFUSED = "This machine credential holds no role it may use"


def refuse_machine_principal(request: Request) -> None:
    """Refuse a token issued to a machine credential's client (403, ``MachinePrincipalRefused``).

    On ``/api/keys``: a credential holding ``owner`` or ``admin`` would otherwise
    mint its own successor before it expires, and so outlive its 365-day cap and
    the membership of the person who created it, and manage its siblings. On the
    role assignment, role and permission definition and member routes: it would
    otherwise change which people hold which roles, or who belongs to its tenant.
    Which tokens are a machine's is the provider's ``machine_client`` reader.
    """
    claims = getattr(request.state, "claims", None)
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if _machine_client(request, claims) is not None:
        raise IdentityErrorRaised(MachinePrincipalRefused(message=MACHINE_PRINCIPAL_REFUSED))


async def _load_machine_memberships(
    request: Request, sub: str, client_id: str, session: AsyncSession
) -> CanonicalMemberships:
    """A machine credential's grants: its roles in its one tenant, and nothing else.

    Refused (403) unless the provider's credential whose tokens carry ``sub`` is
    the one issued to ``client_id``, active and not expired. A deactivated or
    deleted credential's tokens are refused here, before they expire.
    """
    repository = MachineCredentialRepository(session)
    credential = await repository.by_subject(_principal_provider(request), sub)
    if credential is None or credential.client_id != client_id:
        logger.warning("canonical role resolution found no machine credential for sub=%r", sub)
        raise HTTPException(status_code=403, detail=NO_TENANT_CONTEXT)
    if not credentials.in_force(credential, now=datetime.now(timezone.utc)):
        logger.warning("canonical role resolution refused a machine credential not in force (sub=%r)", sub)
        raise HTTPException(status_code=403, detail=NO_TENANT_CONTEXT)
    tenant_id = str(credential.tenant_id)
    tenant = await TenantRepository(session).get(credential.tenant_id)
    active = tenant is not None and tenant.status == TenantStatus.active
    grants = await repository.permissions(credential.id)
    if credentials.holdable_roles(tuple(grants)).is_error():
        # A role deleted or renamed since the credential was made: it holds none, or one no
        # machine may hold (``operator``). Either would serve it more than it was given.
        logger.warning("canonical role resolution refused a machine credential's current roles (sub=%r)", sub)
        raise IdentityErrorRaised(MachinePrincipalRefused(message=MACHINE_ROLES_REFUSED))
    return CanonicalMemberships(
        grants=tuple(
            CanonicalGrant(tenant_id=tenant_id, role_name=role_name, permissions=permissions)
            for role_name, permissions in grants.items()
        ),
        tenants=frozenset({tenant_id}),
        active_tenants=frozenset({tenant_id}) if active else frozenset(),
    )


def is_canonical_principal(request: Request) -> bool:
    """Whether the request's principal is authorized from the canonical model (``CANONICAL_RBAC_PROVIDERS``)."""
    return _principal_provider(request) in CANONICAL_RBAC_PROVIDERS


async def _load_canonical_memberships(
    request: Request,
    claims: dict,
    session: AsyncSession | None,
) -> CanonicalMemberships:
    """Load every grant the canonical model holds for the caller.

    Raises 401 when the token names no subject, and 403 when there is no database,
    no canonical identity for the subject, or the canonical user is not active.
    A tenant that is not active stays among the memberships but out of
    ``active_tenants``, so the resolver serves it to no tenant-scoped request.
    A token issued to a machine credential's client is decided from that
    credential alone (``_load_machine_memberships``), never from a user's
    memberships. Which tenant the request is for is the resolver's decision, not
    this function's.
    """
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject")
    if session is None:
        raise HTTPException(status_code=403, detail=NO_TENANT_CONTEXT)
    machine_client = _machine_client(request, claims)
    if machine_client is not None:
        return await _load_machine_memberships(request, sub, machine_client, session)

    resolver = IdentityResolutionService(
        user_repository=UserRepository(session),
        idp_link_repository=IdPLinkRepository(session),
        provider_repository=ProviderRepository(session),
        assignment_repository=UserTenantRoleRepository(session),
        role_repository=RoleRepository(session),
        tenant_repository=TenantRepository(session),
        # Uncached deliberately: an authorization decision must not be served from
        # an identity snapshot that a role change up to IDENTITY_CACHE_TTL seconds
        # ago has already made wrong. The read path may tolerate that staleness;
        # the authz path may not.
        redis_client=None,
    )
    result = await resolver.resolve(provider=_principal_provider(request), sub=sub)
    if result.is_error():
        logger.debug("canonical role resolution found no identity for sub=%r", sub)
        raise HTTPException(status_code=403, detail=NO_TENANT_CONTEXT)

    # Deactivation is the only revocation signal that reaches this path. Every
    # control that offboards a user — ``UserService.deactivate_user``, the Descope
    # ``user.deleted`` webhook, and reconciliation's ``disabled``/``invited`` mapping
    # — sets ``status`` and leaves the ``user_tenant_roles`` rows intact. For a
    # provider in ``CANONICAL_RBAC_PROVIDERS`` the canonical model is the sole
    # authorization source and nothing disables the upstream account, so a non-active
    # user must be refused here or offboarding revokes nothing at all. Unknown values
    # are refused for the same reason: this fails closed.
    user_status = (result.ok.get("user") or {}).get("status")
    if user_status != UserStatus.active.value:
        logger.warning(
            "canonical role resolution refused a non-active user (status=%r)",
            user_status,
        )
        raise HTTPException(status_code=403, detail=NO_TENANT_CONTEXT)

    user_id = (result.ok.get("user") or {}).get("id")
    tenant_memberships = result.ok.get("tenant_memberships", [])
    return CanonicalMemberships(
        user_id=user_id if isinstance(user_id, str) and user_id else None,
        grants=tuple(
            CanonicalGrant(
                tenant_id=grant["tenant_id"],
                role_name=grant["role_name"],
                permissions=tuple(grant.get("permissions", [])),
            )
            for grant in result.ok.get("roles", [])
        ),
        tenant_names={membership["tenant_id"]: membership["tenant_name"] for membership in tenant_memberships},
        # A suspended tenant, or one whose status is missing or unknown, is not
        # served to its members (``resolve_tenant_access``): the resolver keeps its
        # grants and leaves it out of ``active_tenants``.
        active_tenants=frozenset(
            membership["tenant_id"]
            for membership in tenant_memberships
            if membership.get("status") == TenantStatus.active.value
        ),
    )


async def _canonical_memberships(
    request: Request,
    claims: dict,
    session: AsyncSession | None,
) -> CanonicalMemberships | None:
    """The caller's canonical memberships, or ``None`` for a provider not authorized from them.

    Only a provider in ``CANONICAL_RBAC_PROVIDERS`` is read from the canonical
    model (conventions §18). The memberships are loaded once per request and kept
    on ``request.state``, so a route that depends on both ``get_tenant_id`` and
    ``require_role`` decides both from one read. They are never kept across
    requests.
    """
    if _principal_provider(request) not in CANONICAL_RBAC_PROVIDERS:
        return None
    loaded = getattr(request.state, "canonical_memberships", None)
    if not isinstance(loaded, CanonicalMemberships):
        loaded = await _load_canonical_memberships(request, claims, session)
        request.state.canonical_memberships = loaded
    return loaded


class RbacSessionOverrideError(TypeError):
    """An override of ``get_rbac_session`` is not what ``_rbac_session`` can call.

    Raised for the application's ``dependency_overrides`` entry, which only
    tests set: it is a defect in the override, not in the request.
    """


def _takes_no_argument(provide: object) -> bool:
    """Whether ``provide()`` gets everything it asks for.

    A ``Depends()`` default does not count as one: nothing here resolves it, so
    the override would be handed the marker itself.
    """
    return all(
        (parameter.default is not inspect.Parameter.empty and not isinstance(parameter.default, DependsMarker))
        or parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        for parameter in inspect.signature(provide).parameters.values()
    )


@asynccontextmanager
async def _rbac_session(request: Request) -> AsyncIterator[AsyncSession | None]:
    """Open the ``get_rbac_session`` session now, for a caller that could not declare it.

    ``canonical_memberships`` must not declare ``Depends(get_rbac_session)``:
    FastAPI would then open a session for every request to a tenant-scoped
    route, whoever the provider. Opening it here keeps one definition of that
    session, including whatever the application has overridden it with. An
    override must be what ``get_rbac_session`` is: an async generator function
    that takes no argument and yields once. It is called here, not by FastAPI,
    so it gets no dependencies of its own. An override of another shape raises
    ``RbacSessionOverrideError`` before it is called.
    """
    provide = request.app.dependency_overrides.get(get_rbac_session, get_rbac_session)
    if not (inspect.isasyncgenfunction(provide) and _takes_no_argument(provide)):
        raise RbacSessionOverrideError(
            f"the override of get_rbac_session, {getattr(provide, '__qualname__', provide)!r}, must be an "
            "async generator function that takes no argument: it is called outside FastAPI's dependency injection"
        )
    async with asynccontextmanager(provide)() as session:
        yield session


async def canonical_memberships(request: Request) -> CanonicalMemberships | None:
    """Dependency: the canonical memberships ``get_tenant_id`` resolves the tenant from.

    ``None`` for an unauthenticated request, which ``get_tenant_id`` answers with
    401, and for a provider that is not authorized from the canonical model. For
    those it opens no session and reads nothing: only a provider in
    ``CANONICAL_RBAC_PROVIDERS`` whose memberships this request has not loaded
    yet gets a session, for that one read.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None or _principal_provider(request) not in CANONICAL_RBAC_PROVIDERS:
        return None
    loaded = getattr(request.state, "canonical_memberships", None)
    if isinstance(loaded, CanonicalMemberships):
        return loaded
    async with _rbac_session(request) as session:
        return await _canonical_memberships(request, claims, session)


async def _tenant_access(request: Request, session: AsyncSession | None) -> TenantAccess:
    """The request's tenant and the caller's grants in it. 401 with no claims."""
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    memberships = await _canonical_memberships(request, claims, session)
    return resolve_tenant_access(request, claims, memberships)


def _holds_any(wanted: tuple[str, ...], held: Callable[[TenantAccess], list[str]]) -> Callable[[TenantAccess], bool]:
    """A check that ``held(access)`` contains one of ``wanted``."""

    def holds(access: TenantAccess) -> bool:
        return any(entry in held(access) for entry in wanted)

    return holds


def _roles(access: TenantAccess) -> list[str]:
    return access.roles


def _permissions(access: TenantAccess) -> list[str]:
    return access.permissions


def require_role(*roles: str):
    """Dependency factory that enforces the user has one of the specified roles
    in their current tenant.

    The tenant and the roles come from the tenant resolver (conventions §18):
    a Descope principal's from its ``dct`` + ``tenants`` JWT claims, and those of a
    principal from a provider in ``CANONICAL_RBAC_PROVIDERS`` from the canonical
    model.

    Returns the user's role list for downstream use.
    """
    holds = _holds_any(roles, _roles)

    async def dependency(
        request: Request,
        session: AsyncSession | None = Depends(get_rbac_session),
    ) -> list[str]:
        access = await _tenant_access(request, session)
        if not holds(access):
            raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
        return access.roles

    return dependency


def require_permission(*permissions: str):
    """Dependency factory that enforces the user has one of the specified permissions
    in their current tenant.

    The tenant and the permissions come from the tenant resolver (conventions
    §18): a Descope principal's from its ``dct`` + ``tenants`` JWT claims, and those
    of a principal from a provider in ``CANONICAL_RBAC_PROVIDERS`` from the canonical
    model.

    Returns the user's permission list for downstream use.
    """
    holds = _holds_any(permissions, _permissions)

    async def dependency(
        request: Request,
        session: AsyncSession | None = Depends(get_rbac_session),
    ) -> list[str]:
        access = await _tenant_access(request, session)
        if not holds(access):
            raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
        return access.permissions

    return dependency


def require_global_role(*roles: str, claimed_roles: tuple[str, ...] | None = None):
    """Dependency factory for a route over state that belongs to no tenant.

    Every tenant's users, the provider records and the sync status are not one
    tenant's, so holding one of ``roles`` in one tenant opens all of it. A
    principal from a provider in ``CANONICAL_RBAC_PROVIDERS`` therefore cannot
    pick the tenant that answers, with the ``X-Tenant-ID`` header or with a
    ``dct`` claim on the token: it is decided by the caller's sole membership,
    and a caller with several memberships is refused (conventions §18). A
    Descope principal is decided from its ``dct`` + ``tenants`` claims, as by
    ``require_role``.

    ``claimed_roles``, when given, is what a Descope principal (a recorded
    ``descope_managed`` provider) must hold instead of ``roles``: the role and
    permission definitions take ``owner`` or ``admin`` from a Descope token, as
    they always have, and ``operator`` from the canonical model, where a tenant
    admin's role is one tenant's and the definitions are every tenant's. Any
    other principal decided from its claims (an unrecorded provider, another
    provider's extractor) is refused: its claims are not Descope's.

    Returns the user's role list for downstream use.
    """
    holds_canonical = _holds_any(roles, _roles)
    holds_claimed = holds_canonical if claimed_roles is None else _holds_any(claimed_roles, _roles)

    async def dependency(
        request: Request,
        session: AsyncSession | None = Depends(get_rbac_session),
    ) -> list[str]:
        claims = getattr(request.state, "claims", None)
        if claims is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        memberships = await _canonical_memberships(request, claims, session)
        access = resolve_tenant_access(request, claims, memberships, request_selects=False)
        if memberships is not None:
            holds = holds_canonical
        elif claimed_roles is None:
            holds = holds_claimed
        elif _is_descope_principal(request):
            holds = holds_claimed
        else:
            logger.warning(
                "refused: provider=%s sub=%r holds claims that are not Descope's on a route that reads "
                "only Descope's claimed roles",
                _principal_provider(request) or "unrecorded",
                claims.get("sub") if isinstance(claims, dict) else None,
            )
            raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
        if not holds(access):
            raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
        return access.roles

    return dependency


# The role and permission definitions belong to no tenant and every tenant's grants
# read them: a canonical principal needs ``operator``, as on every other route over
# state that belongs to no tenant, and a Descope principal keeps ``owner`` or
# ``admin`` from its claims (conventions §18).
require_definitions_role = require_global_role("operator", claimed_roles=("owner", "admin"))

# The roles that list the definitions in the request's tenant (``GET /api/roles``,
# ``GET /api/permissions``): listing changes nothing and shows only the global
# definitions, which every tenant's grants read (#707).
DEFINITION_READERS = ("owner", "admin", "operator")


# The role that opens state belonging to no tenant (``require_global_role``).
OPERATOR_ROLE = access_domain.OPERATOR_ROLE

_require_global_operator = require_global_role(OPERATOR_ROLE)


class RoleGrantRefusal(enum.StrEnum):
    """Why a role assignment route refused to assign or remove a role."""

    OPERATOR_GRANT = "Only operators can assign or remove the operator role"
    OPERATOR_DEFINITION = "Only operators can create, change or delete the operator role"


OperatorGrantCheck = Callable[[list[str]], Awaitable[None]]


def _operator_check(request: Request, session: AsyncSession | None, refusal: RoleGrantRefusal) -> OperatorGrantCheck:
    """A check that refuses (403, ``refusal``) when the names include ``operator`` and the caller is not one.

    The caller must hold ``operator`` as ``require_global_role`` decides it: a
    canonical principal by its sole membership, a Descope principal from its
    claims. The check reads nothing when the names do not include ``operator``.
    """

    async def check(role_names: list[str]) -> None:
        if OPERATOR_ROLE not in role_names:
            return
        try:
            await _require_global_operator(request, session)
        except HTTPException as refused:
            if refused.status_code != 403:
                raise
            raise HTTPException(status_code=403, detail=refusal) from refused

    return check


def operator_grant_check(
    request: Request,
    session: AsyncSession | None = Depends(get_rbac_session),
) -> OperatorGrantCheck:
    """Dependency: a check that refuses to assign or remove ``operator`` for a caller who is not one.

    ``operator`` opens every tenant's state (conventions §18), while assigning
    takes only ``owner`` or ``admin`` in one tenant: without this a tenant admin
    could make itself, or anyone, an operator.
    """
    return _operator_check(request, session, RoleGrantRefusal.OPERATOR_GRANT)


def operator_definition_check(
    request: Request,
    session: AsyncSession | None = Depends(get_rbac_session),
) -> OperatorGrantCheck:
    """Dependency: a check that refuses to create, change, rename or delete the ``operator`` role for a non-operator.

    A Descope ``owner`` or ``admin`` passes ``require_definitions_role``: without
    this it could delete ``operator`` (ending it for every operator) or change
    what it grants, or rename another role to or from it.
    """
    return _operator_check(request, session, RoleGrantRefusal.OPERATOR_DEFINITION)


async def caller_user_id(
    request: Request,
    session: AsyncSession | None = Depends(get_rbac_session),
) -> str | None:
    """Dependency: the caller's user id as the member routes spell a member's.

    The canonical user id for a principal authorized from the canonical model
    (read once per request, with its memberships); the token's ``sub`` otherwise,
    which for Descope is the ``userId`` its member routes take. None when there
    is neither.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    memberships = await _canonical_memberships(request, claims, session)
    if memberships is not None:
        return memberships.user_id
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None
