"""The one resolver for "which tenant, which roles, which permissions" (conventions §18).

``get_tenant_id``, ``require_role`` and ``require_permission`` all answer from
``resolve_tenant_access``. It takes its sources in this order:

1. **The provider's claims extractor**, for a provider that is not authorized
   from the canonical model. Descope's reads ``dct`` and ``tenants``. A provider
   with no extractor has no source and is refused.
2. **The request's tenant selector**, for a provider in
   ``CANONICAL_RBAC_PROVIDERS``. The selector is the ``X-Tenant-ID`` header. It
   grants nothing by itself: it only chooses among the tenants the caller's
   canonical memberships already name, and a tenant outside them is refused.
3. **The caller's sole canonical membership**, when the request selects nothing.

On a route whose state belongs to no tenant (``require_global_role``) the request
chooses nothing: a caller with several memberships is refused, whatever the
header or a ``dct`` claim on the token names.

Claims that are ``sole_membership_required`` (a Keycloak token of an
organization-bound type) are believed only with ``confirmed_tenant``: the tenant
the provider's lookup confirmed as the caller's only one. Without it they are
refused, so a caller of the resolver that confirms nothing grants nothing.

Anything else is 403. Nothing here does I/O: the canonical memberships are loaded
by ``app.dependencies.rbac`` and passed in.
"""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig, TenantClaims, descope_tenant_claims

logger = logbook(__name__)

# The request header that names the tenant a canonical-provider request is for.
TENANT_SELECTOR_HEADER = "X-Tenant-ID"

NO_TENANT_CONTEXT = "No tenant context"
# What ``get_tenant_id`` answers when a request has no single tenant, and what
# ``require_descope_principal`` answers so its routes read as they always did.
SELECT_A_TENANT_FIRST = "No tenant context — select a tenant first"
# Its own answer: the token names a tenant, and selecting another would not help.
SOLE_MEMBERSHIP_NOT_CONFIRMED = "This tenant is not confirmed as the caller's only membership"


@dataclass(frozen=True)
class CanonicalGrant:
    """One role the canonical model gives the caller in one tenant, with that role's permissions."""

    tenant_id: str
    role_name: str
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalMemberships:
    """Every grant the canonical model holds for the caller, across all their tenants."""

    grants: tuple[CanonicalGrant, ...]

    @property
    def tenant_ids(self) -> frozenset[str]:
        return frozenset(grant.tenant_id for grant in self.grants)


@dataclass(frozen=True)
class TenantAccess:
    """The resolver's answer: the request's tenant and what the caller holds in it."""

    tenant_id: str
    roles: list[str]
    permissions: list[str]


def _caller(request: Request, claims: dict) -> tuple[str, object]:
    """Who a refusal is logged against: the recorded provider's name and the token's ``sub``."""
    provider = getattr(request.state, "provider", None)
    name = provider.name if isinstance(provider, ProviderTokenConfig) else "unrecorded"
    return name, claims.get("sub")


def _claims_source(request: Request, claims: dict) -> TenantClaims | None:
    """What the request's provider says about the tenant, read with that provider's own extractor.

    ``None`` when the provider declares no extractor, when the token names no
    tenant, when the extractor raises, and when its answer cannot be used (see
    ``_usable``): each is refused by the caller, the last two with a warning.
    A request that no middleware attributed to a provider (one built by hand,
    with only ``request.state.claims`` set) is read with Descope's extractor,
    which is what the claims path has always been. Both middlewares record the
    provider, so that branch is a defect when it is reached in a running
    application, and it is logged as a warning each time.
    """
    provider = getattr(request.state, "provider", None)
    if not isinstance(provider, ProviderTokenConfig):
        logger.warning(
            "tenant resolution: no provider is recorded for sub=%r; its claims are read as Descope's",
            claims.get("sub"),
        )
        extractor = descope_tenant_claims
    elif provider.tenant_claims is None:
        return None
    else:
        extractor = provider.tenant_claims
    try:
        claimed = extractor(claims)
    except Exception:
        # The middlewares answer a raising extractor with 401 before a request
        # gets here. This is the resolver failing closed by itself.
        logger.warning(
            "tenant resolution refused: the claims extractor of provider=%s raised for sub=%r",
            *_caller(request, claims),
            exc_info=True,
        )
        return None
    if isinstance(claimed, TenantClaims) and claimed.tenant_id is None:
        # The token names no tenant: an everyday answer, refused without a warning.
        return None
    if not _usable(claimed):
        logger.warning(
            "tenant resolution refused: the claims extractor of provider=%s "
            "gave an answer that cannot be used for sub=%r",
            *_caller(request, claims),
        )
        return None
    return claimed


def awaiting_confirmation(request: Request, claims: dict) -> TenantAccess | None:
    """What a token says when it is ``sole_membership_required``, before anything confirmed it.

    ``None`` for every other request: one with no recorded provider or no
    extractor, one whose extractor raises or answers with something unusable
    (the resolver refuses and logs those), and one whose claims need no
    confirmation. The answer is not an authorization: it tells the caller which
    tenant to confirm, and lets it answer what the token alone decides first.
    """
    provider = getattr(request.state, "provider", None)
    if not isinstance(provider, ProviderTokenConfig) or provider.tenant_claims is None:
        return None
    try:
        claimed = provider.tenant_claims(claims)
    except Exception:
        return None
    if not _usable(claimed) or claimed.sole_membership_required is not True:
        return None
    return TenantAccess(tenant_id=claimed.tenant_id, roles=claimed.roles, permissions=claimed.permissions)


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(entry, str) for entry in value)


def _usable(claimed: object) -> bool:
    """True when an extractor's answer names a tenant and grants in the shapes the checks rely on.

    The answer must be a ``TenantClaims`` whose tenant is a non-empty string and
    whose roles and permissions are lists of strings. ``require_role`` asks
    ``role in roles``, which on a string is a substring search, so anything but
    a list of strings is refused here and never searched. ``sole_membership_required``
    must be a real boolean: any other value could be read either way.
    """
    return (
        isinstance(claimed, TenantClaims)
        and isinstance(claimed.tenant_id, str)
        and bool(claimed.tenant_id)
        and _is_string_list(claimed.roles)
        and _is_string_list(claimed.permissions)
        and isinstance(claimed.sole_membership_required, bool)
    )


def _selector_value(value: object) -> str:
    """One ``X-Tenant-ID`` value as it is compared with the caller's memberships.

    Surrounding whitespace is dropped. A value that parses as a UUID is put in
    the canonical lower-case hyphenated form the canonical model stores, so an
    upper-case, braced or ``urn:uuid:`` spelling names the same tenant. Anything
    else is compared as it is, and so matches a membership only when it is one.
    """
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    try:
        return str(uuid.UUID(stripped))
    except ValueError:
        return stripped


def _selected_tenants(request: Request, claims: dict) -> tuple[list[str], set[str]]:
    """The tenants this request selects by header and by ``dct`` claim; both empty when it selects nothing.

    The selector is every ``X-Tenant-ID`` value on the request, one entry per
    header line, so a header sent twice is seen as two even with one value. A ``dct`` claim on
    a canonical provider's token selects too: those providers do not normally
    emit one, but a token that carries it has always been held to it. Header and
    claim are put in one spelling (``_selector_value``) before they are compared
    with each other and with the memberships. An empty header value or a ``dct``
    that is not a string is kept as a selection that can match no tenant, so it
    is refused and not treated as absent.
    """
    values = request.headers.getlist(TENANT_SELECTOR_HEADER)
    # Only a real header list counts: a request double in a test has none.
    by_header = [_selector_value(value) for value in values] if isinstance(values, list) else []
    by_claim: set[str] = set()
    claimed = claims.get("dct")
    if claimed is not None and not isinstance(claimed, str):
        by_claim.add("")
    elif claimed:
        by_claim.add(_selector_value(claimed))
    return by_header, by_claim


def _canonical_tenant(
    request: Request, claims: dict, memberships: CanonicalMemberships, *, request_selects: bool
) -> str | None:
    """The one tenant a canonical-provider request is for, or ``None`` when there is not exactly one.

    With ``request_selects`` off, a selection from a caller with several
    memberships is refused, by header and by ``dct`` claim alike: it may name the
    caller's one tenant but never choose among them.
    A refusal of something the request itself selected is logged with the
    provider and the subject, never with the value it sent.
    """
    tenant_ids = memberships.tenant_ids
    by_header, by_claim = _selected_tenants(request, claims)
    selected = set(by_header) | by_claim
    if not selected:
        # Nothing selected: only a sole membership is unambiguous.
        return next(iter(tenant_ids)) if len(tenant_ids) == 1 else None
    if not request_selects and len(tenant_ids) != 1:
        logger.warning(
            "tenant selection refused: provider=%s sub=%r named one of several tenants "
            "on a route whose state belongs to no tenant",
            *_caller(request, claims),
        )
        return None
    if len(by_header) > 1:
        logger.warning(
            "tenant selection refused: provider=%s sub=%r sent more than one tenant selector",
            *_caller(request, claims),
        )
        return None
    if len(selected) > 1:
        logger.warning(
            "tenant selection refused: provider=%s sub=%r sent a header that disagrees with the token's dct",
            *_caller(request, claims),
        )
        return None
    (tenant_id,) = selected
    if tenant_id not in tenant_ids:
        logger.warning(
            "tenant selection refused: provider=%s sub=%r holds no membership in the tenant it names",
            *_caller(request, claims),
        )
        return None
    return tenant_id


def resolve_tenant_access(
    request: Request,
    claims: dict,
    memberships: CanonicalMemberships | None,
    *,
    no_tenant_detail: str = NO_TENANT_CONTEXT,
    request_selects: bool = True,
    confirmed_tenant: str | None = None,
) -> TenantAccess:
    """Resolve the request's tenant and the caller's roles and permissions in it.

    ``memberships`` is the caller's canonical memberships when the request's
    provider is authorized from the canonical model, and ``None`` for every other
    provider. ``request_selects`` is off for a route whose state belongs to no
    tenant: there neither the ``X-Tenant-ID`` header nor a ``dct`` claim may
    choose among several memberships. ``confirmed_tenant`` is the tenant the provider's
    membership lookup confirmed as the caller's only one, for claims that are
    ``sole_membership_required``; such claims are refused (403, with their own
    detail) unless it is the tenant they name. Raises 403 with ``no_tenant_detail`` on every branch that does not
    end at exactly one tenant.
    """
    if memberships is None:
        claimed = _claims_source(request, claims)
        if claimed is None:
            raise HTTPException(status_code=403, detail=no_tenant_detail)
        if claimed.sole_membership_required and confirmed_tenant != claimed.tenant_id:
            logger.warning(
                "tenant resolution refused: provider=%s sub=%r holds a token that needs its tenant "
                "confirmed as the caller's only membership, and it was not",
                *_caller(request, claims),
            )
            raise HTTPException(status_code=403, detail=SOLE_MEMBERSHIP_NOT_CONFIRMED)
        return TenantAccess(tenant_id=claimed.tenant_id, roles=claimed.roles, permissions=claimed.permissions)

    tenant_id = _canonical_tenant(request, claims, memberships, request_selects=request_selects)
    if tenant_id is None:
        raise HTTPException(status_code=403, detail=no_tenant_detail)
    grants = [grant for grant in memberships.grants if grant.tenant_id == tenant_id]
    return TenantAccess(
        tenant_id=tenant_id,
        roles=[grant.role_name for grant in grants],
        permissions=[permission for grant in grants for permission in grant.permissions],
    )
