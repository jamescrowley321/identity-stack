"""Register the Keycloak provider in the canonical `providers` registry.

Keycloak users are provisioned just in time on their first ``GET /api/identity``
(``JIT_ENABLED_PROVIDERS``), which needs this row: the provisioning service
finds the provider by name, ``keycloak``, the lowercased name the token
middleware gives Keycloak principals. Nothing else is imported: a Keycloak
principal's tenant and permissions come from its token, and its organizations
from the realm's Admin API, not from canonical rows.

The row's type is ``oidc``. ``ProviderType`` has no ``keycloak`` member, and
Keycloak is used here as a standard OIDC provider, so no enum migration is
needed.

Idempotent: skips if the ``keycloak`` provider already exists.

Usage:
    DATABASE_URL=postgresql+asyncpg://... \
    KEYCLOAK_ISSUER_URL=http://keycloak.localhost:8180/realms/identity-stack \
        python -m scripts.seed_keycloak [--dry-run]
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

PROVIDER_NAME = "keycloak"


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        print(f"ERROR: {key} environment variable is required", file=sys.stderr)
        sys.exit(1)
    return value


def keycloak_provider(*, issuer_url: str, base_url: str = "", config_ref: str = "") -> Provider:
    """The ``keycloak`` provider row: type ``oidc``, the realm's issuer, single sign-on only."""
    issuer = issuer_url.rstrip("/")
    return Provider(
        name=PROVIDER_NAME,
        type=ProviderType.oidc,
        issuer_url=issuer,
        base_url=base_url or issuer,
        capabilities=["sso"],
        config_ref=config_ref,
    )


async def ensure_keycloak_provider(
    *,
    issuer_url: str,
    base_url: str,
    config_ref: str,
    dry_run: bool,
) -> uuid_mod.UUID | None:
    """Register the Keycloak provider if not already present. Returns provider UUID."""
    async with get_session_factory()() as session:
        result = await session.execute(select(Provider).where(Provider.name == PROVIDER_NAME))
        existing = result.scalars().first()
        if existing:
            print(f"  [skip] Provider '{PROVIDER_NAME}' already exists (id={existing.id})")
            return existing.id

        if dry_run:
            print(f"  [dry-run] Would create provider '{PROVIDER_NAME}'")
            return None

        provider = keycloak_provider(issuer_url=issuer_url, base_url=base_url, config_ref=config_ref)
        session.add(provider)
        await session.commit()
        await session.refresh(provider)
        print(f"  [created] Provider '{PROVIDER_NAME}' (id={provider.id}, issuer={provider.issuer_url})")
        return provider.id


async def main() -> None:
    parser = argparse.ArgumentParser(description="Register the Keycloak provider in the canonical registry")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be created without writing")
    args = parser.parse_args()

    issuer_url = _require_env("KEYCLOAK_ISSUER_URL")
    base_url = os.getenv("KEYCLOAK_BASE_URL", "")
    config_ref = os.getenv("KEYCLOAK_CONFIG_REF", "")

    mode = " (DRY RUN)" if args.dry_run else ""
    print(f"=== Keycloak Provider Registration{mode} ===\n")
    print("1. Ensuring Keycloak provider...")
    try:
        await ensure_keycloak_provider(
            issuer_url=issuer_url,
            base_url=base_url,
            config_ref=config_ref,
            dry_run=args.dry_run,
        )
    except Exception:
        logger.exception("Failed to ensure Keycloak provider")
        print("ERROR: Failed to ensure Keycloak provider", file=sys.stderr)
        sys.exit(1)

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
