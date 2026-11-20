"""The ``tenant_memberships`` an ``IdentityResolutionService`` payload lists beside its grants."""

from app.models.identity.tenant import TenantStatus


def active_memberships(roles: list[dict]) -> list[dict]:
    """One membership in an active tenant for each tenant the grants name, named by its id.

    The real payload lists every tenant a grant names, with that tenant's status,
    and the authorization path serves only the active ones.
    """
    tenant_ids = dict.fromkeys(role["tenant_id"] for role in roles)
    return [
        {"tenant_id": tenant_id, "tenant_name": tenant_id, "status": TenantStatus.active.value}
        for tenant_id in tenant_ids
    ]
