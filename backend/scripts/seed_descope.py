"""Seed canonical identity tables from Descope.

Reads users, tenants, roles, and permissions from the Descope Management API
and writes them to the canonical identity tables (providers, tenants, users,
roles, permissions, role_permissions, user_tenant_roles, idp_links).

Idempotent: skips existing records by unique key. Each entity type is imported
in a separate transaction for partial failure resilience.

Usage:
    DATABASE_URL=postgresql+asyncpg://... \
    DESCOPE_PROJECT_ID=... DESCOPE_MANAGEMENT_KEY=... \
        python -m scripts.seed_descope [--dry-run]
"""

import argparse
import asyncio
import logging
import os
import sys
import uuid as uuid_mod

from sqlmodel import select

# Ensure backend package is importable when run directly (not via -m)
_backend_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.models.database import get_session_factory  # noqa: E402
from app.models.identity.assignment import UserTenantRole  # noqa: E402
from app.models.identity.provider import Provider, ProviderType  # noqa: E402
from app.models.identity.role import Permission, Role, RolePermission  # noqa: E402
from app.models.identity.tenant import Tenant  # noqa: E402
from app.models.identity.user import IdPLink, User, UserStatus  # noqa: E402
from app.services.descope import DescopeManagementClient  # noqa: E402

logger = logging.getLogger(__name__)

# Descope status -> canonical UserStatus
_STATUS_MAP: dict[str, UserStatus] = {
    "enabled": UserStatus.active,
    "disabled": UserStatus.inactive,
    "invited": UserStatus.provisioned,
}


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"ERROR: {key} environment variable is required", file=sys.stderr)
        sys.exit(1)
    return value


async def ensure_descope_provider(*, dry_run: bool) -> uuid_mod.UUID | None:
    """Register the Descope provider if not already present. Returns provider UUID."""
    async with get_session_factory()() as session:
        result = await session.execute(select(Provider).where(Provider.name == "descope"))
        existing = result.scalars().first()
        if existing:
            print(f"  [skip] Provider 'descope' already exists (id={existing.id})")
            return existing.id

        if dry_run:
            print("  [dry-run] Would create provider 'descope'")
            return None

        provider = Provider(
            name="descope",
            type=ProviderType.descope,
            issuer_url=os.getenv("DESCOPE_BASE_URL", "https://api.descope.com"),
            capabilities=["sso", "rbac", "fga"],
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        print(f"  [created] Provider 'descope' (id={provider.id})")
        return provider.id


async def import_tenants(descope_tenants: list[dict], *, dry_run: bool) -> dict[str, uuid_mod.UUID]:
    """Import tenants from Descope. Returns mapping of descope_id -> canonical UUID."""
    tenant_map: dict[str, uuid_mod.UUID] = {}
    created = 0
    skipped = 0

    async with get_session_factory()() as session:
        for dt in descope_tenants:
            descope_id = dt.get("id", "")
            if not descope_id:
                logger.warning("Skipping tenant with no id: %s", dt.get("name", "<unnamed>"))
                continue

            name = dt.get("name", descope_id)
            domains = dt.get("selfProvisioningDomains") or []

            result = await session.execute(select(Tenant).where(Tenant.name == name))
            existing = result.scalars().first()
            if existing:
                if descope_id in tenant_map:
                    logger.warning("Duplicate tenant name '%s' — descope_id %s mapped to existing", name, descope_id)
                tenant_map[descope_id] = existing.id
                skipped += 1
                continue

            if dry_run:
                tenant_map[descope_id] = uuid_mod.uuid4()
                created += 1
                continue

            tenant = Tenant(name=name, domains=domains)
            session.add(tenant)
            await session.flush()
            tenant_map[descope_id] = tenant.id
            created += 1

        if not dry_run:
            await session.commit()

    action = "Would create" if dry_run else "Created"
    print(f"  Tenants: {action} {created}, skipped {skipped}")
    return tenant_map


async def import_permissions(descope_permissions: list[dict], *, dry_run: bool) -> dict[str, uuid_mod.UUID]:
    """Import permissions from Descope. Returns mapping of name -> canonical UUID."""
    perm_map: dict[str, uuid_mod.UUID] = {}
    created = 0
    skipped = 0

    async with get_session_factory()() as session:
        for dp in descope_permissions:
            name = dp.get("name", "")
            if not name:
                continue

            result = await session.execute(select(Permission).where(Permission.name == name))
            existing = result.scalars().first()
            if existing:
                perm_map[name] = existing.id
                skipped += 1
                continue

            if dry_run:
                perm_map[name] = uuid_mod.uuid4()
                created += 1
                continue

            perm = Permission(name=name, description=dp.get("description", ""))
            session.add(perm)
            await session.flush()
            perm_map[name] = perm.id
            created += 1

        if not dry_run:
            await session.commit()

    action = "Would create" if dry_run else "Created"
    print(f"  Permissions: {action} {created}, skipped {skipped}")
    return perm_map


async def import_roles(
    descope_roles: list[dict],
    perm_map: dict[str, uuid_mod.UUID],
    *,
    dry_run: bool,
) -> dict[str, uuid_mod.UUID]:
    """Import roles from Descope. Returns mapping of name -> canonical UUID.

    Also creates role_permissions mappings for each role's permissionNames.
    """
    role_map: dict[str, uuid_mod.UUID] = {}
    created_roles = 0
    skipped_roles = 0
    created_rp = 0
    skipped_rp = 0

    async with get_session_factory()() as session:
        for dr in descope_roles:
            name = dr.get("name", "")
            if not name:
                continue

            # Descope roles are global (tenant_id=NULL)
            result = await session.execute(
                select(Role).where(Role.name == name, Role.tenant_id.is_(None))  # type: ignore[union-attr]
            )
            existing = result.scalars().first()
            if existing:
                role_map[name] = existing.id
                skipped_roles += 1
                role_id = existing.id
            elif dry_run:
                role_id = uuid_mod.uuid4()
                role_map[name] = role_id
                created_roles += 1
            else:
                role = Role(name=name, description=dr.get("description", ""))
                session.add(role)
                await session.flush()
                role_map[name] = role.id
                role_id = role.id
                created_roles += 1

            # Map role -> permissions
            perm_names = dr.get("permissionNames") or []
            for pname in perm_names:
                pid = perm_map.get(pname)
                if not pid:
                    logger.warning("Permission '%s' not found for role '%s', skipping", pname, name)
                    continue

                result = await session.execute(
                    select(RolePermission).where(
                        RolePermission.role_id == role_id,
                        RolePermission.permission_id == pid,
                    )
                )
                if result.scalars().first():
                    skipped_rp += 1
                    continue

                if dry_run:
                    created_rp += 1
                    continue

                rp = RolePermission(role_id=role_id, permission_id=pid)
                session.add(rp)
                created_rp += 1

        if not dry_run:
            await session.commit()

    action = "Would create" if dry_run else "Created"
    print(f"  Roles: {action} {created_roles}, skipped {skipped_roles}")
    print(f"  RolePermissions: {action} {created_rp}, skipped {skipped_rp}")
    return role_map


async def import_users(descope_users: list[dict], *, dry_run: bool) -> dict[str, uuid_mod.UUID]:
    """Import users from Descope. Returns mapping of descope_user_id -> canonical UUID."""
    user_map: dict[str, uuid_mod.UUID] = {}
    created = 0
    skipped = 0

    async with get_session_factory()() as session:
        for du in descope_users:
            descope_user_id = du.get("userId", "")
            email = du.get("email", "")
            if not email:
                logger.warning("Skipping user %s with no email", descope_user_id)
                continue

            result = await session.execute(select(User).where(User.email == email))
            existing = result.scalars().first()
            if existing:
                user_map[descope_user_id] = existing.id
                skipped += 1
                continue

            if dry_run:
                user_map[descope_user_id] = uuid_mod.uuid4()
                created += 1
                continue

            status_str = du.get("status", "enabled")
            if status_str not in _STATUS_MAP:
                logger.warning("Unknown Descope status '%s' for user %s, defaulting to active", status_str, email)
            user = User(
                email=email,
                user_name=du.get("name", email),
                given_name=du.get("givenName", ""),
                family_name=du.get("familyName", ""),
                status=_STATUS_MAP.get(status_str, UserStatus.active),
            )
            session.add(user)
            await session.flush()
            user_map[descope_user_id] = user.id
            created += 1

        if not dry_run:
            await session.commit()

    action = "Would create" if dry_run else "Created"
    print(f"  Users: {action} {created}, skipped {skipped}")
    return user_map


async def import_user_tenant_roles(
    descope_users: list[dict],
    user_map: dict[str, uuid_mod.UUID],
    tenant_map: dict[str, uuid_mod.UUID],
    role_map: dict[str, uuid_mod.UUID],
    *,
    dry_run: bool,
) -> None:
    """Import user-tenant-role assignments from Descope user data."""
    created = 0
    skipped = 0

    async with get_session_factory()() as session:
        for du in descope_users:
            descope_user_id = du.get("userId", "")
            canonical_user_id = user_map.get(descope_user_id)
            if not canonical_user_id:
                continue

            for ut in du.get("userTenants") or []:
                descope_tenant_id = ut.get("tenantId", "")
                canonical_tenant_id = tenant_map.get(descope_tenant_id)
                if not canonical_tenant_id:
                    continue

                for role_name in ut.get("roleNames") or []:
                    canonical_role_id = role_map.get(role_name)
                    if not canonical_role_id:
                        logger.warning(
                            "Role '%s' not found for user %s in tenant %s",
                            role_name,
                            descope_user_id,
                            descope_tenant_id,
                        )
                        continue

                    result = await session.execute(
                        select(UserTenantRole).where(
                            UserTenantRole.user_id == canonical_user_id,
                            UserTenantRole.tenant_id == canonical_tenant_id,
                            UserTenantRole.role_id == canonical_role_id,
                        )
                    )
                    if result.scalars().first():
                        skipped += 1
                        continue

                    if dry_run:
                        created += 1
                        continue

                    utr = UserTenantRole(
                        user_id=canonical_user_id,
                        tenant_id=canonical_tenant_id,
                        role_id=canonical_role_id,
                    )
                    session.add(utr)
                    created += 1

        if not dry_run:
            await session.commit()

    action = "Would create" if dry_run else "Created"
    print(f"  UserTenantRoles: {action} {created}, skipped {skipped}")


async def import_idp_links(
    descope_users: list[dict],
    user_map: dict[str, uuid_mod.UUID],
    provider_id: uuid_mod.UUID | None,
    *,
    dry_run: bool,
) -> None:
    """Create IdP links mapping canonical users to their Descope identities."""
    if provider_id is None and not dry_run:
        print("  [skip] No provider_id — cannot create IdP links")
        return

    created = 0
    skipped = 0

    async with get_session_factory()() as session:
        for du in descope_users:
            descope_user_id = du.get("userId", "")
            canonical_user_id = user_map.get(descope_user_id)
            if not canonical_user_id:
                continue

            # Check for existing links (skip in dry-run when provider is new —
            # no links can exist yet for a not-yet-created provider)
            if provider_id is not None:
                result = await session.execute(
                    select(IdPLink).where(
                        IdPLink.provider_id == provider_id,
                        IdPLink.external_sub == descope_user_id,
                    )
                )
                if result.scalars().first():
                    skipped += 1
                    continue

            if dry_run:
                created += 1
                continue

            link = IdPLink(
                user_id=canonical_user_id,
                provider_id=provider_id,
                external_sub=descope_user_id,
                external_email=du.get("email", ""),
            )
            session.add(link)
            created += 1

        if not dry_run:
            await session.commit()

    action = "Would create" if dry_run else "Created"
    print(f"  IdPLinks: {action} {created}, skipped {skipped}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed canonical identity tables from Descope")
    parser.add_argument("--dry-run", action="store_true", help="Count what would be imported without writing")
    args = parser.parse_args()

    project_id = _require_env("DESCOPE_PROJECT_ID")
    management_key = _require_env("DESCOPE_MANAGEMENT_KEY")
    base_url = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")

    client = DescopeManagementClient(project_id, management_key, base_url)

    mode = " (DRY RUN)" if args.dry_run else ""
    print(f"=== Descope → Canonical Identity Seed{mode} ===\n")

    # 1. Ensure Descope provider exists
    print("1. Ensuring Descope provider...")
    try:
        provider_id = await ensure_descope_provider(dry_run=args.dry_run)
    except Exception:
        logger.exception("Failed to ensure Descope provider")
        print("ERROR: Failed to ensure Descope provider", file=sys.stderr)
        sys.exit(1)

    # 2. Fetch Descope data
    print("\n2. Fetching data from Descope...")
    try:
        descope_tenants = await client.list_tenants()
        print(f"   Tenants: {len(descope_tenants)}")
    except Exception:
        logger.exception("Failed to fetch tenants")
        print("ERROR: Failed to fetch tenants from Descope", file=sys.stderr)
        sys.exit(1)

    try:
        descope_permissions = await client.list_permissions()
        print(f"   Permissions: {len(descope_permissions)}")
    except Exception:
        logger.exception("Failed to fetch permissions")
        print("ERROR: Failed to fetch permissions from Descope", file=sys.stderr)
        sys.exit(1)

    try:
        descope_roles = await client.list_roles()
        print(f"   Roles: {len(descope_roles)}")
    except Exception:
        logger.exception("Failed to fetch roles")
        print("ERROR: Failed to fetch roles from Descope", file=sys.stderr)
        sys.exit(1)

    try:
        descope_users = await client.search_all_users()
        print(f"   Users: {len(descope_users)}")
    except Exception:
        logger.exception("Failed to fetch users")
        print("ERROR: Failed to fetch users from Descope", file=sys.stderr)
        sys.exit(1)

    # 3. Import entities (each in its own transaction)
    print("\n3. Importing tenants...")
    try:
        tenant_map = await import_tenants(descope_tenants, dry_run=args.dry_run)
    except Exception:
        logger.exception("Failed to import tenants")
        print("ERROR: Tenant import failed (partial data may have been committed)", file=sys.stderr)
        tenant_map = {}

    print("\n4. Importing permissions...")
    try:
        perm_map = await import_permissions(descope_permissions, dry_run=args.dry_run)
    except Exception:
        logger.exception("Failed to import permissions")
        print("ERROR: Permission import failed (partial data may have been committed)", file=sys.stderr)
        perm_map = {}

    print("\n5. Importing roles and role-permission mappings...")
    try:
        role_map = await import_roles(descope_roles, perm_map, dry_run=args.dry_run)
    except Exception:
        logger.exception("Failed to import roles")
        print("ERROR: Role import failed (partial data may have been committed)", file=sys.stderr)
        role_map = {}

    print("\n6. Importing users...")
    try:
        user_map = await import_users(descope_users, dry_run=args.dry_run)
    except Exception:
        logger.exception("Failed to import users")
        print("ERROR: User import failed (partial data may have been committed)", file=sys.stderr)
        user_map = {}

    print("\n7. Importing user-tenant-role assignments...")
    try:
        await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=args.dry_run)
    except Exception:
        logger.exception("Failed to import user-tenant-role assignments")
        print("ERROR: UserTenantRole import failed (partial data may have been committed)", file=sys.stderr)

    print("\n8. Importing IdP links...")
    try:
        await import_idp_links(descope_users, user_map, provider_id, dry_run=args.dry_run)
    except Exception:
        logger.exception("Failed to import IdP links")
        print("ERROR: IdPLink import failed (partial data may have been committed)", file=sys.stderr)

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
