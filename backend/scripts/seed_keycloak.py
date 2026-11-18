"""Register the Keycloak provider in the canonical `providers` registry, and link tenants to organizations.

Keycloak users are provisioned just in time on their first ``GET /api/identity``
when ``keycloak`` is in ``JIT_ENABLED_PROVIDERS`` (opt-in), which needs this row: the provisioning service
finds the provider by name, ``keycloak``, the lowercased name the token
middleware gives Keycloak principals. A first login joins, as ``member``, the
one active tenant whose ``external_org_id`` is the alias in the token's
``organization`` claim and whose ``external_org_provider_id`` is this row.

With ``--link-organizations`` the seed also reads a JSON object of canonical
tenant name to organization alias on standard input (the ``organization_aliases``
output of ``infra/keycloak``, narrowed to the tenants a canonical tenant names;
``make keycloak-seed`` does both) and links each tenant: it creates the tenant
when missing, and sets ``external_org_id`` to the alias on an existing tenant
that is linked to nothing. It refuses, writing nothing, a tenant linked to
another organization or for another provider, an unlinked tenant that has
``domains``, an alias another tenant holds, and a tenant that is not active.
It writes no ``domains``: a first login is admitted only for an email in its
tenant's domains, so until an operator writes them only users linked
beforehand sign in, as with ``scripts/seed_zitadel.py``.

The row's type is ``oidc``. ``ProviderType`` has no ``keycloak`` member, and
Keycloak is used here as a standard OIDC provider, so no enum migration is
needed.

Idempotent: skips the provider if it already exists, and a tenant already linked to its alias.

Run it as a module from ``backend/``, so ``app`` is importable.

Usage:
    DATABASE_URL=postgresql+asyncpg://... \
    KEYCLOAK_ISSUER_URL=http://keycloak.localhost:8180/realms/identity-stack \
        python -m scripts.seed_keycloak [--dry-run]
    terraform -chdir=infra/keycloak output -json organization_aliases | ... \
        | python -m scripts.seed_keycloak --link-organizations
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid as uuid_mod

from sqlmodel import select

from app.models.database import get_session_factory
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.tenant import Tenant, TenantStatus

logger = logging.getLogger(__name__)

PROVIDER_NAME = "keycloak"


class OrganizationsInputError(ValueError):
    """Standard input is not a JSON object of tenant name to organization alias."""


class OrganizationLinkConflict(RuntimeError):
    """The canonical store holds a link the seed would have to take over from someone else."""


def parse_organizations(raw: object) -> dict[str, str]:
    """A non-empty JSON object of tenant name to non-empty organization alias, else ``OrganizationsInputError``."""
    if not isinstance(raw, dict) or not raw:
        raise OrganizationsInputError("the organizations must be a non-empty JSON object of tenant name to alias")
    for name, alias in raw.items():
        if not isinstance(alias, str) or not alias:
            raise OrganizationsInputError(f"the alias of tenant {name!r} must be a non-empty string")
    return dict(raw)


def link_tenant(tenant: Tenant | None, name: str, alias: str, provider_id: uuid_mod.UUID) -> Tenant:
    """The tenant ``name`` linked to the organization ``alias`` of the provider ``provider_id``.

    A missing tenant is created, with no ``domains``; one linked to nothing gets
    the link. One linked elsewhere, one with ``domains`` it would be linked with
    (they would admit its organization's first logins, and were chosen for
    another purpose), or one not active raises ``OrganizationLinkConflict``.
    """
    if tenant is None:
        return Tenant(
            name=name, external_org_id=alias, external_org_provider_id=provider_id, status=TenantStatus.active
        )
    if tenant.external_org_id is None and tenant.external_org_provider_id is None:
        if tenant.domains:
            raise OrganizationLinkConflict(
                f"tenant {name!r} has domains, which would admit its organization's first logins; "
                "clear tenants.domains or link another tenant"
            )
        tenant.external_org_id = alias
        tenant.external_org_provider_id = provider_id
    elif (tenant.external_org_id, tenant.external_org_provider_id) != (alias, provider_id):
        raise OrganizationLinkConflict(f"tenant {name!r} is already linked to another organization or provider")
    if tenant.status != TenantStatus.active:
        raise OrganizationLinkConflict(f"tenant {name!r} is {tenant.status.value}, not {TenantStatus.active.value}")
    return tenant


async def link_organizations(*, provider_id: uuid_mod.UUID, organizations: dict[str, str], dry_run: bool) -> None:
    """Link each tenant to its organization alias in one transaction: a refusal, or a dry run, writes nothing."""
    async with get_session_factory()() as session:
        for name, alias in sorted(organizations.items()):
            holders = (
                await session.execute(select(Tenant.name).where(Tenant.external_org_id == alias, Tenant.name != name))
            ).scalars()
            holder = holders.first()
            if holder is not None:
                raise OrganizationLinkConflict(f"organization {alias!r} is already linked to tenant {holder!r}")
            tenant = (await session.execute(select(Tenant).where(Tenant.name == name))).scalars().first()
            session.add(link_tenant(tenant, name, alias, provider_id))
            print(f"  [{'dry-run' if dry_run else 'linked'}] tenant {name!r} to organization {alias!r}")
        if dry_run:
            await session.rollback()
        else:
            await session.commit()


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
    parser.add_argument(
        "--link-organizations",
        action="store_true",
        help="Also link tenants to organizations, from a JSON object of tenant name to alias on standard input",
    )
    args = parser.parse_args()
    organizations: dict[str, str] = {}
    if args.link_organizations:
        try:
            organizations = parse_organizations(json.load(sys.stdin))
        except (json.JSONDecodeError, OrganizationsInputError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    issuer_url = _require_env("KEYCLOAK_ISSUER_URL")
    base_url = os.getenv("KEYCLOAK_BASE_URL", "")
    config_ref = os.getenv("KEYCLOAK_CONFIG_REF", "")

    mode = " (DRY RUN)" if args.dry_run else ""
    print(f"=== Keycloak Provider Registration{mode} ===\n")
    print("1. Ensuring Keycloak provider...")
    try:
        provider_id = await ensure_keycloak_provider(
            issuer_url=issuer_url,
            base_url=base_url,
            config_ref=config_ref,
            dry_run=args.dry_run,
        )
    except Exception:
        logger.exception("Failed to ensure Keycloak provider")
        print("ERROR: Failed to ensure Keycloak provider", file=sys.stderr)
        sys.exit(1)

    if organizations and provider_id is None:
        print("2. [dry-run] Would link tenants to organizations once the provider exists:", sorted(organizations))
    elif organizations and provider_id is not None:
        print("2. Linking tenants to organizations...")
        try:
            await link_organizations(provider_id=provider_id, organizations=organizations, dry_run=args.dry_run)
        except OrganizationLinkConflict as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
