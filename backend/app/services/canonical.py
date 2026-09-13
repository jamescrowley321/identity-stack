"""Canonical field names over Descope's own payload shapes.

Descope returns ``userId``/``createdTime``/``modifiedTime``; the rest of this API
speaks ``id``/``created_at``/``updated_at``. These helpers add the canonical names
*beside* the upstream ones rather than replacing them, so a caller reading either
vocabulary keeps working.

Timestamps come back as Unix epoch seconds and are emitted as UTC ISO-8601, which
is how every canonical model on this side stores them.
"""

from datetime import datetime, timezone


def _iso(epoch_seconds: object) -> str | None:
    """Render Descope's epoch-seconds timestamp as UTC ISO-8601."""
    if not isinstance(epoch_seconds, (int, float)) or isinstance(epoch_seconds, bool):
        return None
    if epoch_seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def canonical_user(user: object) -> object:
    """Add canonical id/created_at/updated_at to a Descope user payload."""
    if not isinstance(user, dict):
        return user
    out = dict(user)
    user_id = user.get("userId")
    if user_id:
        out["id"] = user_id
    created = _iso(user.get("createdTime"))
    if created:
        out["created_at"] = created
    # A user Descope has never modified reports no modifiedTime; created_at is then
    # the correct updated_at, not a missing value.
    out["updated_at"] = _iso(user.get("modifiedTime")) or created or None
    if out["updated_at"] is None:
        del out["updated_at"]
    return out


def canonical_tenant(tenant: object) -> object:
    """Add canonical created_at/updated_at to a Descope tenant payload.

    ``id`` is already the tenant's Descope id and is left as it is. Descope does
    not report a modification time for tenants at all, so updated_at mirrors
    created_at — the standard reading for a resource with no modification
    tracking. It is NOT the canonical ``tenants.updated_at`` column: no
    Descope-tenant-id to canonical-UUID lookup exists (``Tenant.external_org_id``
    maps Ory Organizations, and ``Tenant.name`` is the only other unique column).
    """
    if not isinstance(tenant, dict):
        return tenant
    out = dict(tenant)
    created = _iso(tenant.get("createdTime"))
    if created:
        out["created_at"] = created
        out["updated_at"] = _iso(tenant.get("modifiedTime")) or created
    return out
