"""Provider memberships an ending raced with: shared by RoleService and UserService.

For an adapter that keeps memberships (``IdentityProviderAdapter.ends_memberships``),
a membership is ended after the canonical change commits. A role assigned in the
tenant meanwhile pushed its membership before the removal reached the provider,
so the removal undid it. Counted after every removal, a role assigned later
pushes its own membership.
"""

from __future__ import annotations

import uuid

from opentelemetry import trace

from app.logbook import logbook
from app.repositories.assignment import UserTenantRoleRepository
from app.services.adapters.base import IdentityProviderAdapter

logger = logbook(__name__)
tracer = trace.get_tracer(__name__)


async def restore_memberships(
    assignments: UserTenantRoleRepository,
    adapter: IdentityProviderAdapter,
    ended: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    """Add back each just-ended (user, tenant) membership whose user holds a role in the tenant again.

    One query for every pair; one membership push per pair that holds a role. A
    failed push is logged as every sync failure is.
    """
    with tracer.start_as_current_span("memberships.restore_memberships") as span:
        span.set_attribute("memberships.ended", len(ended))
        if not ended:
            return
        restored: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for row in await assignments.list_by_user_tenants(ended):
            if (row.user_id, row.tenant_id) in restored:
                continue
            restored.add((row.user_id, row.tenant_id))
            pushed = await adapter.sync_role_assignment(
                user_id=row.user_id, tenant_id=row.tenant_id, role_id=row.role_id
            )
            if pushed.is_error():
                logger.warning(
                    "IdP sync failed for role %s (restore_membership): %s",
                    row.role_id,
                    pushed.error.message,
                )
