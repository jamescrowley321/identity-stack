"""Seed script for FGA document access control demo.

Creates three sample documents with different access patterns:
- public-roadmap: all tenant members get viewer relation
- board-minutes: only owner-role users get access
- team-project: specific users get editor, others get viewer

Idempotent for DB documents (skips existing by title). FGA relations
tolerate duplicates but do not remove stale relations from prior runs.

Usage:
    DESCOPE_PROJECT_ID=... DESCOPE_MANAGEMENT_KEY=... DEMO_TENANT_ID=... \
        python -m scripts.seed_demo
"""

import asyncio
import os
import sys

import httpx
from sqlmodel import select

# Ensure backend package is importable when run from backend/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import async_engine, async_session_factory  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.services.descope import DescopeManagementClient  # noqa: E402

# Demo document definitions
DEMO_DOCUMENTS = [
    {
        "title": "public-roadmap",
        "content": "Q3 2026 product roadmap — shared with all team members.",
    },
    {
        "title": "board-minutes",
        "content": "Board meeting minutes — restricted to owners only.",
    },
    {
        "title": "team-project",
        "content": "Cross-functional project plan — editors and viewers assigned individually.",
    },
]


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"ERROR: {key} environment variable is required", file=sys.stderr)
        sys.exit(1)
    return value


async def _get_or_create_documents(tenant_id: str, owner_user_id: str) -> list[Document]:
    """Ensure demo documents exist in the DB. Returns all three (existing or new)."""
    from sqlmodel import SQLModel

    async with async_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    documents = []
    new_docs: list[Document] = []

    async with async_session_factory() as session:
        for doc_def in DEMO_DOCUMENTS:
            result = await session.execute(
                select(Document).where(
                    Document.tenant_id == tenant_id,
                    Document.title == doc_def["title"],
                )
            )
            existing = result.scalars().first()

            if existing:
                print(f"  [skip] '{doc_def['title']}' already exists (id={existing.id})")
                documents.append(existing)
            else:
                doc = Document(
                    tenant_id=tenant_id,
                    title=doc_def["title"],
                    content=doc_def["content"],
                    created_by=owner_user_id,
                )
                session.add(doc)
                new_docs.append(doc)

        if new_docs:
            await session.commit()
            for doc in new_docs:
                await session.refresh(doc)
                print(f"  [created] '{doc.title}' (id={doc.id})")
                documents.append(doc)

    return documents


async def _seed_fga_relations(
    client: DescopeManagementClient,
    documents: list[Document],
    tenant_users: list[dict],
    owner_user_id: str,
) -> None:
    """Create FGA relations for the demo scenario."""
    if not documents:
        print("  No documents to seed relations for.")
        return

    # Find tenant users by role
    owners = []
    non_owners = []
    for user in tenant_users:
        user_id = user.get("userId", "")
        if not user_id:
            continue
        tenant_roles = []
        for t in user.get("userTenants", []):
            if t.get("tenantId") == documents[0].tenant_id:
                tenant_roles = t.get("roleNames", [])
                break
        if "owner" in tenant_roles or "admin" in tenant_roles:
            owners.append(user_id)
        else:
            non_owners.append(user_id)

    doc_map = {d.title: d for d in documents}

    # public-roadmap: all tenant members get viewer
    roadmap = doc_map["public-roadmap"]
    print(f"\n  Seeding relations for '{roadmap.title}':")
    await _ensure_relation(client, roadmap.id, "owner", owner_user_id)
    for user in tenant_users:
        uid = user.get("userId", "")
        if uid != owner_user_id:
            await _ensure_relation(client, roadmap.id, "viewer", uid)

    # board-minutes: only owners get access
    minutes = doc_map["board-minutes"]
    print(f"\n  Seeding relations for '{minutes.title}':")
    await _ensure_relation(client, minutes.id, "owner", owner_user_id)
    for uid in owners:
        if uid != owner_user_id:
            await _ensure_relation(client, minutes.id, "viewer", uid)

    # team-project: first non-owner gets editor, rest get viewer
    project = doc_map["team-project"]
    print(f"\n  Seeding relations for '{project.title}':")
    await _ensure_relation(client, project.id, "owner", owner_user_id)
    for i, uid in enumerate(non_owners):
        relation = "editor" if i == 0 else "viewer"
        await _ensure_relation(client, project.id, relation, uid)


async def _ensure_relation(
    client: DescopeManagementClient,
    document_id: str,
    relation: str,
    target: str,
) -> None:
    """Create a relation, tolerating duplicates (Descope returns 400)."""
    try:
        await client.create_relation("document", document_id, relation, target)
        print(f"    [created] {relation} -> {target}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400:
            print(f"    [skip] {relation} -> {target} (already exists)")
        else:
            raise


async def main() -> None:
    project_id = _require_env("DESCOPE_PROJECT_ID")
    management_key = _require_env("DESCOPE_MANAGEMENT_KEY")
    tenant_id = _require_env("DEMO_TENANT_ID")
    base_url = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")

    try:
        client = DescopeManagementClient(project_id, management_key, base_url)
    except Exception:
        print("ERROR: Failed to initialize Descope client", file=sys.stderr)
        sys.exit(1)

    print("=== FGA Demo Seed ===\n")

    # 1. Discover tenant users
    print("1. Loading tenant users...")
    try:
        tenant_users = await client.search_tenant_users(tenant_id)
    except Exception:
        print("ERROR: Failed to load tenant users", file=sys.stderr)
        sys.exit(1)
    if not tenant_users:
        print("ERROR: No users found in tenant. Add users first.", file=sys.stderr)
        sys.exit(1)
    print(f"   Found {len(tenant_users)} users")

    # Use first owner/admin as document creator, fall back to first user
    owner_user_id = tenant_users[0].get("userId", "")
    found_owner = False
    for user in tenant_users:
        for t in user.get("userTenants", []):
            roles = t.get("roleNames", [])
            if t.get("tenantId") == tenant_id and ("owner" in roles or "admin" in roles):
                owner_user_id = user.get("userId", "")
                found_owner = True
                break
        if found_owner:
            break

    # 2. Create documents in DB
    print("\n2. Ensuring demo documents exist in DB...")
    documents = await _get_or_create_documents(tenant_id, owner_user_id)

    # 3. Seed FGA relations
    print("\n3. Seeding FGA relations...")
    await _seed_fga_relations(client, documents, tenant_users, owner_user_id)

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
