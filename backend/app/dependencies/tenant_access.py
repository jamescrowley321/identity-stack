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

Anything else is 403. Nothing here does I/O: the canonical memberships are loaded
by ``app.dependencies.rbac`` and passed in.
"""

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.logbook import logbook
from app.middleware.providers import ProviderTokenConfig, TenantClaims, descope_tenant_claims

logger = logbook(__name__)

# The request header that names the tenant a canonical-provider request is for.
TENANT_SELECTOR_HEADER = "X-Tenant-ID"

NO_TENANT_CONTEXT = "No tenant context"


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


def _claims_source(request: Request, claims: dict) -> TenantClaims | None:
    """What the request's provider says about the tenant, read with that provider's own extractor.

    ``None`` when the provider declares no extractor. A request that no
    middleware attributed to a provider (one built by hand, with only
    ``request.state.claims`` set) is read with Descope's extractor, which is what
    the claims path has always been.
    """
    provider = getattr(request.state, "provider", None)
    if not isinstance(provider, ProviderTokenConfig):
        return descope_tenant_claims(claims)
    if provider.tenant_claims is None:
        return None
    return provider.tenant_claims(claims)


def _selected_tenants(request: Request, claims: dict) -> set[str] | None:
    """The tenants this request selects, or ``None`` when it selects nothing.

    The selector is every ``X-Tenant-ID`` value on the request. A ``dct`` claim on
    a canonical provider's token selects too: those providers do not normally
    emit one, but a token that carries it has always been held to it. An empty
    value or a ``dct`` that is not a string is kept as a selection that can match
    no tenant, so it is refused and not treated as absent.
    """
    selected: set[str] = set()
    values = request.headers.getlist(TENANT_SELECTOR_HEADER)
    # Only a real header list counts: a request double in a test has none.
    if isinstance(values, list):
        selected.update(value.strip() if isinstance(value, str) else "" for value in values)
    claimed = claims.get("dct")
    if claimed is not None and not isinstance(claimed, str):
        selected.add("")
    elif claimed:
        selected.add(claimed)
    return selected or None


def _canonical_tenant(request: Request, claims: dict, memberships: CanonicalMemberships) -> str | None:
    """The one tenant a canonical-provider request is for, or ``None`` when there is not exactly one."""
    tenant_ids = memberships.tenant_ids
    selected = _selected_tenants(request, claims)
    if selected is None:
        # Nothing selected: only a sole membership is unambiguous.
        return next(iter(tenant_ids)) if len(tenant_ids) == 1 else None
    if len(selected) != 1:
        logger.warning("tenant selection refused: the request names more than one tenant")
        return None
    (tenant_id,) = selected
    if tenant_id not in tenant_ids:
        logger.warning("tenant selection refused: the caller holds no membership in the tenant it names")
        return None
    return tenant_id


def resolve_tenant_access(
    request: Request,
    claims: dict,
    memberships: CanonicalMemberships | None,
    *,
    no_tenant_detail: str = NO_TENANT_CONTEXT,
) -> TenantAccess:
    """Resolve the request's tenant and the caller's roles and permissions in it.

    ``memberships`` is the caller's canonical memberships when the request's
    provider is authorized from the canonical model, and ``None`` for every other
    provider. Raises 403 with ``no_tenant_detail`` on every branch that does not
    end at exactly one tenant.
    """
    if memberships is None:
        claimed = _claims_source(request, claims)
        if claimed is None or not claimed.tenant_id:
            raise HTTPException(status_code=403, detail=no_tenant_detail)
        return TenantAccess(tenant_id=claimed.tenant_id, roles=claimed.roles, permissions=claimed.permissions)

    tenant_id = _canonical_tenant(request, claims, memberships)
    if tenant_id is None:
        raise HTTPException(status_code=403, detail=no_tenant_detail)
    grants = [grant for grant in memberships.grants if grant.tenant_id == tenant_id]
    return TenantAccess(
        tenant_id=tenant_id,
        roles=[grant.role_name for grant in grants],
        permissions=[permission for grant in grants for permission in grant.permissions],
    )
