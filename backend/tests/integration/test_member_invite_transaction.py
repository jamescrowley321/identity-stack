"""An invitation's user and roles commit together, against the test Postgres (#677).

``MemberService`` over the shipped services and repositories on one session, as
``get_member_directory`` wires it; the adapter is a mock, since the point is what
Postgres keeps when a role fails after the user and an earlier role were written.
"""

import uuid
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.errors.identity import IdentityError
from app.models.identity.assignment import UserTenantRole
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import User
from app.repositories.assignment import UserTenantRoleRepository
from app.repositories.permission import PermissionRepository
from app.repositories.role import RoleRepository
from app.repositories.user import UserRepository
from app.services.adapters.base import IdentityProviderAdapter
from app.services.role import RoleService
from app.services.user import UserService


async def test_a_role_failing_mid_invitation_leaves_no_user_and_no_assignment(db_session):
    acme = Tenant(name=f"acme-{uuid.uuid4()}", status=TenantStatus.active)
    globex = Tenant(name=f"globex-{uuid.uuid4()}", status=TenantStatus.active)
    db_session.add_all([acme, globex])
    await db_session.flush()
    member = Role(name=f"member-{uuid.uuid4().hex[:8]}")
    globex_only = Role(name=f"editor-{uuid.uuid4().hex[:8]}", tenant_id=globex.id)
    db_session.add_all([member, globex_only])
    await db_session.commit()
    # Read before the invitation: its rollback expires every loaded row.
    acme_id, member_id, globex_only_id = acme.id, member.id, globex_only.id

    adapter = AsyncMock(spec=IdentityProviderAdapter)
    adapter.ends_memberships = True
    assignments = UserTenantRoleRepository(db_session)
    users = UserService(
        repository=UserRepository(db_session),
        adapter=adapter,
        assignment_repository=assignments,
        publisher=AsyncMock(),
    )
    roles = RoleService(
        repository=RoleRepository(db_session),
        permission_repository=PermissionRepository(db_session),
        assignment_repository=assignments,
        adapter=adapter,
        publisher=AsyncMock(),
    )
    email = f"rolled-back-{uuid.uuid4().hex[:12]}@example.com"

    # The member role is written first; globex's own role is out of acme's scope.
    created = await users.create_member(
        tenant_id=acme_id,
        email=email,
        stage_memberships=lambda user_id: roles.stage_member_roles(
            user_id=user_id, tenant_id=acme_id, role_ids=[member_id, globex_only_id]
        ),
    )

    assert created.is_error() and isinstance(created.error, IdentityError)
    assert (await db_session.execute(select(User).where(User.email == email))).scalars().all() == []
    held = await db_session.execute(select(UserTenantRole).where(UserTenantRole.role_id == member_id))
    assert held.scalars().all() == []
    adapter.sync_user.assert_not_awaited()
    adapter.sync_role_assignment.assert_not_awaited()
    # What was committed before the invitation is still there.
    assert await db_session.get(Role, member_id) is not None
