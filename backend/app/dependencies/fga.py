import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.dependencies.identity import get_relationship_store
from app.dependencies.rbac import canonical_memberships
from app.dependencies.tenant import get_tenant_id
from app.dependencies.tenant_access import NO_TENANT_CONTEXT, CanonicalMemberships, resolve_tenant_access
from app.logbook import logbook
from app.services.relationship_store import RelationshipStoreError, RelationshipStoreErrorKind
from app.services.relationship_users import DirectoryUser, UserDirectory, UserDirectoryUnavailable

logger = logbook(__name__)


def extract_user_id(request: Request) -> str:
    """Extract user_id from JWT claims on request.state.

    Raises 401 if claims are missing, not a dict, or lack a 'sub' field.
    Shared by require_fga dependency and document router endpoints.
    """
    claims = getattr(request.state, "claims", None)
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user identity")
    return user_id


@dataclass(frozen=True)
class RelationshipCaller:
    """Whom the FGA and document routes act for: the request's tenant and the caller's subject id in the store."""

    tenant_id: str
    user_id: str


def _subject(request: Request, memberships: CanonicalMemberships | None) -> str:
    """The caller's id in the relationship store.

    A Descope principal's is its ``sub``, the Descope user id Descope's store keys
    on. A principal authorized from the canonical model is keyed by its canonical
    user id, never its provider's ``sub`` (``RELATIONSHIP_STORES`` pairs each store
    with the principals whose ids it holds).
    """
    if memberships is None:
        return extract_user_id(request)
    if not memberships.user_id:
        raise HTTPException(status_code=403, detail=NO_TENANT_CONTEXT)
    return memberships.user_id


async def get_relationship_caller(
    request: Request,
    tenant_id: Annotated[str, Depends(get_tenant_id)],
    memberships: Annotated[CanonicalMemberships | None, Depends(canonical_memberships)] = None,
) -> RelationshipCaller:
    """Dependency: the request's tenant from the resolver (conventions §18) and the caller's subject id."""
    return RelationshipCaller(tenant_id=tenant_id, user_id=_subject(request, memberships))


async def _relationship_caller(request: Request) -> RelationshipCaller:
    """``get_relationship_caller`` for ``require_fga``, which resolves its own dependencies.

    The subject first, so a token without one is 401 before it is 403 for its tenant.
    """
    claims = getattr(request.state, "claims", None)
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Not authenticated")
    memberships = await canonical_memberships(request)
    user_id = _subject(request, memberships)
    return RelationshipCaller(tenant_id=resolve_tenant_access(request, claims, memberships).tenant_id, user_id=user_id)


def store_error_response(exc: RelationshipStoreError) -> HTTPException:
    """What a route answers for a failed store call. ``ABSENT`` and ``UNAVAILABLE`` are the store's fault."""
    status = {
        RelationshipStoreErrorKind.INVALID: 422,
        RelationshipStoreErrorKind.REFUSED: 400,
        RelationshipStoreErrorKind.SCHEMA_READ_ONLY: 409,
    }.get(exc.kind, 502)
    return HTTPException(status_code=status, detail=exc.detail)


async def find_user(directory: UserDirectory, identifier: str) -> DirectoryUser | None:
    """``directory.find``, with a directory that cannot answer as 502."""
    try:
        return await directory.find(identifier)
    except UserDirectoryUnavailable as exc:
        logger.error("Failed to look up a relation's target user: %s", exc.upstream)
        raise HTTPException(status_code=502, detail="Failed to verify target user") from exc


async def resolve_fga_user_target(directory: UserDirectory, identifier: str, tenant_id: str) -> str:
    """Resolve a user identifier to the user id the relationship store keys on, if it names one.

    Every FGA *read* in this service keys on the caller's subject id
    (``_subject``): ``require_fga`` checks with it, and the owner tuple is written
    from it. A write that keys on whatever the caller happened to type therefore
    lands on a target no read will ever match: sharing by a username returned 200
    while granting nothing at all, and revoking reported success while removing
    nothing. Resolving caller-supplied targets in the store's own user directory
    (Descope's users for Descope's store, the canonical users for OpenFGA) makes
    both ends of the tuple agree.

    Best-effort by design: an identifier that names no user is returned
    unchanged. ``/api/fga/relations`` and ``/api/fga/check`` are the raw admin
    surface over the authz store, where a target need not be a user at all — a
    permission check for a subject that does not exist must be answerable
    ("allowed: false"), not a 404. Endpoints that *require* the target to be a
    real user — document share and revoke — establish that themselves and answer
    404 before they get here.

    Scoped to ``tenant_id``: a user the caller's tenant does not contain is
    treated exactly like a user that does not exist, and the identifier falls
    through unresolved. Without that check these routes resolved by login id
    across every tenant, which made them an existence oracle over every account —
    a hit echoed back the victim's user id while a miss echoed the input
    unchanged — and let an admin of one tenant write a real grant keyed on a
    subject belonging to another.

    Falling through is deliberate, rather than answering 403. A distinct status
    for "exists, but not in your tenant" would rebuild the same oracle in the
    status code. Out-of-tenant and nonexistent are made indistinguishable, and
    the resulting tuple keys on the raw identifier, which no read matches — so
    it confers nothing on the real user.

    ``tenant_id`` is required, not optional: a caller that has no tenant to scope
    by must not reach this function.

    A directory that cannot answer is never silently swallowed (502): only a
    clean "no such user" falls through to the identifier.
    """
    try:
        user = await directory.find(identifier)
    except UserDirectoryUnavailable as exc:
        logger.error("Failed to resolve FGA target: %s", exc.upstream)
        raise HTTPException(status_code=502, detail="Failed to resolve target user") from exc
    if user is None:
        return identifier
    if tenant_id not in user.tenant_ids:
        logger.info("FGA target resolved to a user outside the caller's tenant; leaving it unresolved")
        return identifier
    return user.user_id


def require_fga(
    resource_type: str,
    relation: str,
    *,
    resource_id_param: str = "document_id",
    resource_id_pattern: str | None = None,
):
    """Dependency factory that enforces an FGA permission check.

    Takes the tenant from the resolver (conventions §18) and the caller's subject
    id (``_subject``), and the resource id from the path parameter named by
    ``resource_id_param``, then checks the relation in the deployment's
    relationship store.

    Returns the caller's subject id for downstream use.
    Fail-closed: any FGA API error results in HTTP 502 (deny).

    ``resource_id_pattern`` is checked here rather than left to the path
    parameter's own ``Path(pattern=...)``. FastAPI solves dependencies before it
    validates the endpoint's own parameters, so a malformed id reached the FGA
    call first and came back 403 "denied" — Descope cannot hold a relation for a
    syntactically impossible resource — and the 422 the path parameter would have
    raised was never reported. Validating here makes the response describe the
    real fault and spares the upstream authz call.
    """
    compiled = re.compile(resource_id_pattern) if resource_id_pattern else None

    async def dependency(request: Request) -> str:
        caller = await _relationship_caller(request)
        user_id, tenant_id = caller.user_id, caller.tenant_id
        resource_id = request.path_params.get(resource_id_param, "")
        if not resource_id:
            raise HTTPException(status_code=400, detail="Missing resource identifier")
        if compiled is not None and not compiled.fullmatch(resource_id):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {resource_id_param}: expected {resource_id_pattern}",
            )
        try:
            store = get_relationship_store(request)
            allowed = await store.check(tenant_id, resource_type, resource_id, relation, user_id)
        except RelationshipStoreError as exc:
            # Include the store's answer. Without it a refusal here says only "the
            # authz store refused", and the reason — a relation the schema does
            # not define, a malformed identifier — is nowhere in the logs, which
            # is what made a drifted FGA schema undiagnosable from CI alone.
            logger.error(
                "FGA check failed (%s) user=%s resource=%s:%s relation=%s upstream=%s",
                exc.kind,
                user_id,
                resource_type,
                resource_id,
                relation,
                exc.upstream,
            )
            raise HTTPException(status_code=502, detail="Authorization check failed") from exc
        if not allowed:
            logger.warning(
                "FGA denied: user=%s resource_type=%s resource_id=%s relation=%s",
                user_id,
                resource_type,
                resource_id,
                relation,
            )
            raise HTTPException(status_code=403, detail="Access denied")
        return user_id

    return dependency
