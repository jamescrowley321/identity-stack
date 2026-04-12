#!/usr/bin/env python3
"""One-time cleanup of orphaned access keys in Descope.

These keys were created before the keyTenants fix in PR #266 and have
empty keyTenants: []. They won't appear in any tenant-scoped search
but still exist in the Descope project.

Usage:
    cd backend && python -m scripts.cleanup_orphaned_keys

Requires DESCOPE_PROJECT_ID and DESCOPE_MANAGEMENT_KEY in environment
(or .env file).

See: https://github.com/jamescrowley321/identity-stack/issues/269
"""

import asyncio
import os
import sys

# Add backend to path so we can import the Descope client
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ORPHANED_KEY_IDS = [
    "K3CGRIrGdowPMusDgBoSuUD7gmd0",  # demo-seed-key
    "K3CGRUKGoKEYYnCt0GcJAW4qZNfg",  # demo-seed-key
    "K3CGROnr6Wqyhu4OADo1HqsLnOAl",  # demo-seed-key
    "K3CGVHonKLe9jSSYDvykJCkHWQp6",  # demo-seed-key
    "K3CGYQBwIQQ4wGnlQLF1f0IQ4nAU",  # playwright-test-key
]


async def main() -> None:
    from app.services.descope import DescopeManagementClient

    project_id = os.environ.get("DESCOPE_PROJECT_ID")
    mgmt_key = os.environ.get("DESCOPE_MANAGEMENT_KEY")

    if not project_id or not mgmt_key:
        print("Error: DESCOPE_PROJECT_ID and DESCOPE_MANAGEMENT_KEY must be set")
        sys.exit(1)

    client = DescopeManagementClient(project_id=project_id, management_key=mgmt_key)

    print(f"Deleting {len(ORPHANED_KEY_IDS)} orphaned access keys...")
    for key_id in ORPHANED_KEY_IDS:
        try:
            await client.delete_access_key(key_id)
            print(f"  ✓ Deleted {key_id}")
        except Exception as exc:
            print(f"  ✗ Failed to delete {key_id}: {exc}")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
