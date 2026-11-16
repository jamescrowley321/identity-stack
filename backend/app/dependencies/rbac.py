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

A third shape carries authorization in the token but not all of it: a Keycloak
token of an organization-bound type holds only for a caller whose one
organization is the token's. This module asks the provider's membership lookup
for those tokens, after everything the token alone decides: no lookup wired or
an answer of "no" is 403, and a lookup that fails, or does not answer within
``MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS``, is 502 and no grant. "After"
holds however a route declares its dependencies: ``get_tenant_id``'s own
dependency applies the checks of every gate on the route before it asks.
"""

import asyncio
import inspect
import math
import weakref
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.params import Depends as DependsMarker
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.tenant_access import (
    NO_TENANT_CONTEXT,
    CanonicalGrant,
    CanonicalMemberships,
    TenantAccess,
    awaiting_confirmation,
    resolve_tenant_access,
)
from app.knobs import knob, knob_set
from app.logbook import logbook
from app.middleware.providers import ProviderConfigurationError, ProviderTokenConfig
from app.models.database import get_session_factory
from app.models.identity.user import UserStatus
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.idp_link import IdPLinkRepository
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

# The check each ``require_*`` gate makes of a caller's grants, by the gate's
# dependency function. ``confirmed_tenant`` finds a route's gates here.
_GATE_CHECKS: "weakref.WeakKeyDictionary[Callable, Callable[[TenantAccess], bool]]" = weakref.WeakKeyDictionary()
# The provider's directory could not say which tenants the caller belongs to.
MEMBERSHIP_LOOKUP_FAILED = "Membership lookup failed"


def membership_lookup_timeout() -> float:
    """``MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS`` as seconds: how long a request waits for a membership lookup.

    Raises ``ProviderConfigurationError`` for a value that is not a positive,
    finite number: a limit that cannot be read is not replaced by a guess.
    """
    raw = knob("MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS", "5")
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not (math.isfinite(seconds) and seconds > 0):
        raise ProviderConfigurationError(
            f"MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS must be a positive number of seconds, not {raw!r}"
        )
    return seconds


# Fixed when this module is imported, so a value that is no limit stops startup.
MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS = membership_lookup_timeout()


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


def _principal_provider(request: Request) -> str:
    """The name of the provider that authenticated this request, lowercased."""
    principal = getattr(request.state, "principal", None)
    identity = getattr(principal, "identity", None)
    authentication_type = getattr(identity, "authentication_type", None)
    return authentication_type.lower() if isinstance(authentication_type, str) else ""


async def _load_canonical_memberships(
    request: Request,
    claims: dict,
    session: AsyncSession | None,
) -> CanonicalMemberships:
    """Load every grant the canonical model holds for the caller.

    Raises 401 when the token names no subject, and 403 when there is no database,
    no canonical identity for the subject, or the canonical user is not active.
    Which tenant the request is for is the resolver's decision, not this function's.
    """
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject")
    if session is None:
        raise HTTPException(status_code=403, detail=NO_TENANT_CONTEXT)

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

    return CanonicalMemberships(
        grants=tuple(
            CanonicalGrant(
                tenant_id=grant["tenant_id"],
                role_name=grant["role_name"],
                permissions=tuple(grant.get("permissions", [])),
            )
            for grant in result.ok.get("roles", [])
        )
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


@dataclass(frozen=True)
class _SoleMembership:
    """The tenant this request's lookup confirmed as the caller's only one. Never kept across requests."""

    tenant_id: str


async def _confirm_sole_membership(request: Request, claims: dict, tenant_id: str) -> str | None:
    """``tenant_id`` when the provider's lookup says it is the caller's one tenant, else ``None``.

    For claims that are ``sole_membership_required`` (conventions §18). ``None``
    is a refusal the resolver answers with 403: no lookup is wired for the
    provider, or the caller belongs to no tenant, to another one, or to several.
    A lookup that raises, that does not answer within
    ``MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS`` (it is cancelled then), or that answers
    with anything but a collection of strings, could not be asked: that is 502
    and no grant, as for the FGA store (§17). A
    confirmation is kept on ``request.state`` for the rest of the request, so a
    route that depends on both ``get_tenant_id`` and ``require_permission`` asks
    once.
    """
    kept = getattr(request.state, "sole_membership", None)
    if isinstance(kept, _SoleMembership) and kept.tenant_id == tenant_id:
        return tenant_id
    provider = getattr(request.state, "provider", None)
    lookup = provider.sole_membership_lookup if isinstance(provider, ProviderTokenConfig) else None
    name = provider.name if isinstance(provider, ProviderTokenConfig) else "unrecorded"
    sub = claims.get("sub")
    if lookup is None:
        logger.warning("membership not confirmed: provider=%s has no membership lookup (sub=%r)", name, sub)
        return None
    limit = MEMBERSHIP_LOOKUP_TIMEOUT_SECONDS
    bound = asyncio.timeout(limit)
    try:
        async with bound:
            tenants = await lookup(claims)
    except Exception as exc:
        if bound.expired():
            logger.warning("membership lookup of provider=%s did not answer within %s s (sub=%r)", name, limit, sub)
        else:
            logger.warning("membership lookup failed for provider=%s sub=%r", name, sub, exc_info=True)
        raise HTTPException(status_code=502, detail=MEMBERSHIP_LOOKUP_FAILED) from exc
    if not isinstance(tenants, (list, tuple, set, frozenset)) or not all(isinstance(entry, str) for entry in tenants):
        logger.warning("membership lookup of provider=%s gave an answer that cannot be used (sub=%r)", name, sub)
        raise HTTPException(status_code=502, detail=MEMBERSHIP_LOOKUP_FAILED)
    if set(tenants) != {tenant_id}:
        logger.warning(
            "membership not confirmed: provider=%s sub=%r belongs to %d tenant(s), not to the token's alone",
            name,
            sub,
            len(set(tenants)),
        )
        return None
    request.state.sole_membership = _SoleMembership(tenant_id)
    return tenant_id


def _is_key_of(registered: Mapping, call: object) -> bool:
    """Whether ``call`` is a key of ``registered``. A callable that cannot be hashed is a key of nothing."""
    try:
        return call in registered
    except TypeError:
        return False


def _route_gate_checks(request: Request) -> list[Callable[[TenantAccess], bool]]:
    """The check of every ``require_*`` gate the request's route depends on, at any depth.

    Read from the route FastAPI matched, so it is the same list whether a gate
    is declared in ``dependencies=[...]``, as a parameter, or on the router. A
    gate the application has overridden is left out, and so is everything
    under an overridden dependency: FastAPI runs the override in its place, and
    none of what it depended on. Empty for a request that no route was matched
    for (one built by hand).
    """
    scope = getattr(request, "scope", None)
    route = scope.get("route") if isinstance(scope, dict) else None
    pending = list(getattr(getattr(route, "dependant", None), "dependencies", ()))
    if not pending:
        return []
    overridden = request.app.dependency_overrides
    checks = []
    while pending:
        dependant = pending.pop()
        call = dependant.call
        if _is_key_of(overridden, call):
            continue
        pending.extend(dependant.dependencies)
        if _is_key_of(_GATE_CHECKS, call):
            checks.append(_GATE_CHECKS[call])
    return checks


async def confirmed_tenant(request: Request) -> str | None:
    """Dependency: the tenant ``get_tenant_id`` may believe a ``sole_membership_required`` token for.

    ``None`` for every request whose claims need no confirmation, without a
    lookup: an unauthenticated one, a provider authorized from the canonical
    model, and a token that names no tenant or settles its claims alone. Also
    ``None`` when the lookup did not confirm the token's tenant, which
    ``get_tenant_id`` then refuses. Raises 502 when the lookup fails.

    FastAPI may resolve this before the route's ``require_*`` gate (a gate
    declared as a parameter after ``get_tenant_id``). What the token alone
    decides still comes first: a token that one of the route's gates would
    refuse is refused here (403), and the lookup is not asked.
    """
    claims = getattr(request.state, "claims", None)
    if not isinstance(claims, dict) or _principal_provider(request) in CANONICAL_RBAC_PROVIDERS:
        return None
    unconfirmed = awaiting_confirmation(request, claims)
    if unconfirmed is None:
        return None
    if not all(holds(unconfirmed) for holds in _route_gate_checks(request)):
        raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
    return await _confirm_sole_membership(request, claims, unconfirmed.tenant_id)


async def _tenant_access(
    request: Request,
    session: AsyncSession | None,
    *,
    request_selects: bool = True,
    holds: Callable[[TenantAccess], bool] | None = None,
) -> TenantAccess:
    """The request's tenant and the caller's grants in it. 401 with no claims.

    ``holds`` is the caller's own check of the grants. For a token that needs
    its tenant confirmed it is applied first, to what the token alone says: a
    token that would be refused anyway is refused (403) without a lookup, so a
    directory that is down never turns that 403 into a 502.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    memberships = await _canonical_memberships(request, claims, session)
    confirmed = None
    if memberships is None:
        unconfirmed = awaiting_confirmation(request, claims)
        if unconfirmed is not None:
            if holds is not None and not holds(unconfirmed):
                raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
            confirmed = await _confirm_sole_membership(request, claims, unconfirmed.tenant_id)
    return resolve_tenant_access(
        request, claims, memberships, request_selects=request_selects, confirmed_tenant=confirmed
    )


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
    model. A Keycloak token carries permissions and no role, so a Keycloak
    principal is always refused here.

    Returns the user's role list for downstream use.
    """
    holds = _holds_any(roles, _roles)

    async def dependency(
        request: Request,
        session: AsyncSession | None = Depends(get_rbac_session),
    ) -> list[str]:
        access = await _tenant_access(request, session, holds=holds)
        if not holds(access):
            raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
        return access.roles

    _GATE_CHECKS[dependency] = holds
    return dependency


def require_permission(*permissions: str):
    """Dependency factory that enforces the user has one of the specified permissions
    in their current tenant.

    The tenant and the permissions come from the tenant resolver (conventions
    §18): a Descope principal's from its ``dct`` + ``tenants`` JWT claims, and those
    of a principal from a provider in ``CANONICAL_RBAC_PROVIDERS`` from the canonical
    model. A Keycloak principal's are the client roles of its token's one
    audience, in the organization the token names.

    Returns the user's permission list for downstream use.
    """
    holds = _holds_any(permissions, _permissions)

    async def dependency(
        request: Request,
        session: AsyncSession | None = Depends(get_rbac_session),
    ) -> list[str]:
        access = await _tenant_access(request, session, holds=holds)
        if not holds(access):
            raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
        return access.permissions

    _GATE_CHECKS[dependency] = holds
    return dependency


def require_global_role(*roles: str):
    """Dependency factory for a route over state that belongs to no tenant.

    Every tenant's users, the provider records and the sync status are not one
    tenant's, so holding one of ``roles`` in one tenant opens all of it. A
    principal from a provider in ``CANONICAL_RBAC_PROVIDERS`` therefore cannot
    pick the tenant that answers, with the ``X-Tenant-ID`` header or with a
    ``dct`` claim on the token: it is decided by the caller's sole membership,
    and a caller with several memberships is refused (conventions §18). A
    Descope principal is decided from its ``dct`` + ``tenants`` claims, as by
    ``require_role``.

    Returns the user's role list for downstream use.
    """
    holds = _holds_any(roles, _roles)

    async def dependency(
        request: Request,
        session: AsyncSession | None = Depends(get_rbac_session),
    ) -> list[str]:
        access = await _tenant_access(request, session, request_selects=False, holds=holds)
        if not holds(access):
            raise HTTPException(status_code=403, detail=INSUFFICIENT_PERMISSIONS)
        return access.roles

    _GATE_CHECKS[dependency] = holds
    return dependency
