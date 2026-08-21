"""Register the Ory provider in the canonical `providers` registry.

Ory is greenfield (no users/tenants/roles to import), so unlike ``seed_descope``
this only ensures the ``ory`` provider row exists — pointing at the live Ory
project issuer so token validation and identity resolution can find it. Roles and
permissions stay canonical (not sourced from Ory).

Idempotent: skips if the ``ory`` provider already exists.

Usage:
    DATABASE_URL=postgresql+asyncpg://... \
    ORY_ISSUER_URL=https://<slug>.projects.oryapis.com \
        python -m scripts.seed_ory [--dry-run]
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
from app.models.identity.provider import Provider, ProviderType  # noqa: E402

logger = logging.getLogger(__name__)


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"ERROR: {key} environment variable is required", file=sys.stderr)
        sys.exit(1)
    return value


async def ensure_ory_provider(
    *,
    issuer_url: str,
    base_url: str,
    config_ref: str,
    dry_run: bool,
) -> uuid_mod.UUID | None:
    """Register the Ory provider if not already present. Returns provider UUID."""
    async with get_session_factory()() as session:
        result = await session.execute(select(Provider).where(Provider.name == "ory"))
        existing = result.scalars().first()
        if existing:
            print(f"  [skip] Provider 'ory' already exists (id={existing.id})")
            return existing.id

        if dry_run:
            print("  [dry-run] Would create provider 'ory'")
            return None

        provider = Provider(
            name="ory",
            type=ProviderType.ory,
            issuer_url=issuer_url,
            base_url=base_url,
            # Ory is used as an OIDC provider; RBAC/permissions stay canonical.
            capabilities=["sso"],
            config_ref=config_ref,
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        print(f"  [created] Provider 'ory' (id={provider.id}, issuer={issuer_url})")
        return provider.id


async def main() -> None:
    parser = argparse.ArgumentParser(description="Register the Ory provider in the canonical registry")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be created without writing")
    args = parser.parse_args()

    issuer_url = _require_env("ORY_ISSUER_URL").rstrip("/")
    base_url = os.getenv("ORY_BASE_URL", issuer_url)
    config_ref = os.getenv("ORY_CONFIG_REF", "")

    mode = " (DRY RUN)" if args.dry_run else ""
    print(f"=== Ory Provider Registration{mode} ===\n")
    print("1. Ensuring Ory provider...")
    try:
        await ensure_ory_provider(
            issuer_url=issuer_url,
            base_url=base_url,
            config_ref=config_ref,
            dry_run=args.dry_run,
        )
    except Exception:
        logger.exception("Failed to ensure Ory provider")
        print("ERROR: Failed to ensure Ory provider", file=sys.stderr)
        sys.exit(1)

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
