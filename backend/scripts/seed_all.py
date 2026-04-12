"""Unified seed script — populates every resource type for local development.

Orchestrates the existing seed scripts and adds the missing resources so
every page in the frontend has data to display.

Pipeline:
  1. seed_descope — providers, tenants, permissions, roles, role_permissions,
                    users, user_tenant_roles, idp_links (from live Descope)
  2. tenant_resources — demo resources per tenant (local DB only)
  3. access_keys — create a demo access key in Descope (first tenant)
  4. seed_demo — documents + FGA relations (requires DEMO_TENANT_ID, which
                 this script auto-discovers from the first tenant)

Idempotent: each stage skips existing records.

Usage (inside docker compose):
    make seed

Direct:
    cd backend && python -m scripts.seed_all
"""

import asyncio
import os
import sys

# Ensure backend package is importable
_backend_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from sqlmodel import select  # noqa: E402

from app.models.database import get_session_factory  # noqa: E402
from app.models.tenant import TenantResource  # noqa: E402
from app.services.descope import DescopeManagementClient  # noqa: E402

# Re-use existing seed logic
from scripts.seed_descope import (  # noqa: E402
    ensure_descope_provider,
    import_idp_links,
    import_permissions,
    import_roles,
    import_tenants,
    import_user_tenant_roles,
    import_users,
)


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"ERROR: {key} environment variable is required", file=sys.stderr)
        sys.exit(1)
    return value


DEMO_RESOURCES = [
    {"name": "Staging API Server", "description": "API server for the staging environment"},
    {"name": "Production Database", "description": "Primary PostgreSQL database for production"},
    {"name": "Redis Cache Cluster", "description": "Shared Redis cluster for session and cache data"},
    {"name": "CI/CD Pipeline", "description": "GitHub Actions workflow for automated builds and deploys"},
    {"name": "Monitoring Dashboard", "description": "Grafana dashboard for service health and metrics"},
]


async def seed_tenant_resources(tenant_ids: list[str]) -> None:
    """Create demo tenant resources in each tenant."""
    created = 0
    skipped = 0

    async with get_session_factory()() as session:
        for tid in tenant_ids:
            for res_def in DEMO_RESOURCES:
                result = await session.execute(
                    select(TenantResource).where(
                        TenantResource.tenant_id == tid,
                        TenantResource.name == res_def["name"],
                    )
                )
                if result.scalars().first():
                    skipped += 1
                    continue

                resource = TenantResource(
                    tenant_id=tid,
                    name=res_def["name"],
                    description=res_def["description"],
                )
                session.add(resource)
                created += 1

        await session.commit()

    print(f"  TenantResources: Created {created}, skipped {skipped}")


async def seed_access_keys(
    client: DescopeManagementClient,
    tenant_id: str,
) -> None:
    """Create a demo access key in Descope for the given tenant."""
    key_name = "demo-seed-key"

    try:
        existing = await client.search_access_keys(tenant_id)
        for key in existing:
            if key.get("name") == key_name:
                print(f"  [skip] Access key '{key_name}' already exists (id={key.get('id', 'unknown')})")
                return
    except Exception:
        print("  [warn] Could not search existing access keys — attempting create anyway")

    try:
        result = await client.create_access_key(
            name=key_name,
            tenant_id=tenant_id,
            role_names=["viewer"],
        )
        key_id = result.get("key", {}).get("keyId", "unknown")
        print(f"  [created] Access key '{key_name}' (id={key_id})")
    except Exception as e:
        print(f"  [warn] Could not create access key: {e}")


async def main() -> None:
    project_id = _require_env("DESCOPE_PROJECT_ID")
    management_key = _require_env("DESCOPE_MANAGEMENT_KEY")
    base_url = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")

    client = DescopeManagementClient(project_id, management_key, base_url)

    print("=" * 60)
    print("  IDENTITY STACK — FULL SEED")
    print("=" * 60)

    # ── Phase 1: Identity data from Descope ──
    print("\n── Phase 1: Descope → canonical identity tables ──\n")

    provider_id = await ensure_descope_provider(dry_run=False)

    print("\n  Fetching data from Descope...")
    try:
        descope_tenants = await client.list_tenants()
        descope_permissions = await client.list_permissions()
        descope_roles = await client.list_roles()
        descope_users = await client.search_all_users()
    except Exception as e:
        print(f"ERROR: Failed to fetch from Descope: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"    Tenants: {len(descope_tenants)}, Permissions: {len(descope_permissions)}, "
        f"Roles: {len(descope_roles)}, Users: {len(descope_users)}"
    )

    tenant_map = await import_tenants(descope_tenants, dry_run=False)
    perm_map = await import_permissions(descope_permissions, dry_run=False)
    role_map = await import_roles(descope_roles, perm_map, dry_run=False)
    user_map = await import_users(descope_users, dry_run=False)
    await import_user_tenant_roles(descope_users, user_map, tenant_map, role_map, dry_run=False)
    await import_idp_links(descope_users, user_map, provider_id, dry_run=False)

    # ── Phase 2: Tenant resources ──
    print("\n── Phase 2: Tenant resources ──\n")

    # Use Descope tenant IDs (the ones the frontend's JWT claims reference)
    descope_tenant_ids = [dt.get("id", "") for dt in descope_tenants if dt.get("id")]
    await seed_tenant_resources(descope_tenant_ids)

    # ── Phase 3: Access keys ──
    print("\n── Phase 3: Access keys ──\n")

    if descope_tenant_ids:
        first_tenant_id = descope_tenant_ids[0]
        first_tenant_name = next(
            (dt.get("name", first_tenant_id) for dt in descope_tenants if dt.get("id") == first_tenant_id),
            first_tenant_id,
        )
        print(f"  Creating access key for tenant '{first_tenant_name}'...")
        await seed_access_keys(client, first_tenant_id)
    else:
        print("  [skip] No tenants available — cannot create access keys")

    # ── Phase 4: Documents + FGA ──
    print("\n── Phase 4: Documents + FGA relations ──\n")

    if descope_tenant_ids:
        # Find the tenant that has actual users (may not be the first one)
        demo_tenant_id = None
        demo_tenant_name = None
        tenant_users: list[dict] = []

        for tid in descope_tenant_ids:
            tname = next(
                (dt.get("name", tid) for dt in descope_tenants if dt.get("id") == tid),
                tid,
            )
            try:
                users = await client.search_tenant_users(tid)
                if users:
                    demo_tenant_id = tid
                    demo_tenant_name = tname
                    tenant_users = users
                    break
                print(f"  Tenant '{tname}' has no users, trying next...")
            except Exception:
                print(f"  [warn] Could not search users in tenant '{tname}'")

        if demo_tenant_id and tenant_users:
            os.environ["DEMO_TENANT_ID"] = demo_tenant_id
            print(f"  Using tenant '{demo_tenant_name}' (id={demo_tenant_id}, {len(tenant_users)} users)")

            try:
                from scripts.seed_demo import _get_or_create_documents, _seed_fga_relations

                # Pick owner/admin as document creator
                owner_user_id = tenant_users[0].get("userId", "")
                for user in tenant_users:
                    for t in user.get("userTenants", []):
                        roles = t.get("roleNames", [])
                        if t.get("tenantId") == demo_tenant_id and ("owner" in roles or "admin" in roles):
                            owner_user_id = user.get("userId", "")
                            break

                documents = await _get_or_create_documents(demo_tenant_id, owner_user_id)
                await _seed_fga_relations(client, documents, tenant_users, owner_user_id)
            except Exception as e:
                print(f"  [warn] FGA seed failed (non-fatal): {e}")
        else:
            print("  [skip] No tenants with users found — cannot create documents")
    else:
        print("  [skip] No tenants — cannot create demo documents")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  SEED COMPLETE")
    print("=" * 60)

    async with get_session_factory()() as session:
        from app.models.document import Document
        from app.models.identity.assignment import UserTenantRole
        from app.models.identity.provider import Provider
        from app.models.identity.role import Permission, Role, RolePermission
        from app.models.identity.tenant import Tenant
        from app.models.identity.user import IdPLink, User

        counts = {}
        for model_cls in [
            Provider,
            Tenant,
            Permission,
            Role,
            RolePermission,
            User,
            UserTenantRole,
            IdPLink,
            TenantResource,
            Document,
        ]:
            result = await session.execute(select(model_cls))
            counts[model_cls.__tablename__] = len(result.scalars().all())

        print("\n  Database contents:")
        for table, count in counts.items():
            print(f"    {table}: {count}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
