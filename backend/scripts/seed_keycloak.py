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
The links are the provider's state (nothing is marked for reconciliation to
push): the organizations already exist, and pushing the tenants back would
overwrite their names and domains. The seed writes no ``domains``; the next
reconciliation pass takes each linked organization's name, domains and status
into its tenant, and a first login is admitted for an email in those domains.

With ``--link-users`` the seed instead reads a JSON object of username to
account (``external_sub``, the Keycloak user id, and ``email``, ``given_name``,
``family_name``) on standard input and links each account to a canonical user:
it creates the user with an ``idp_links`` row for ``keycloak``, as the provider's
state (nothing is marked for reconciliation to push), and leaves a user already
linked to the account as it is, whatever its status. It refuses, writing
nothing, an email a user holds without a link to that account.
It assigns no role: a reconciliation pass (``POST /api/internal/reconciliation/run``
with ``SYNC_PROVIDER=keycloak``) takes each linked user's role assignments, and
the realm's permissions and roles, from Keycloak. ``make keycloak-seed`` links
the fixture users that hold a role in a linked tenant.

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
    ... | python -m scripts.seed_keycloak --link-users
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid as uuid_mod
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.database import get_session_factory
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import LINK_ORIGIN, LINKED_BY_BACKEND, IdPLink, User, UserStatus
from app.repositories.sync_ledger import PendingPushRepository

logger = logging.getLogger(__name__)

PROVIDER_NAME = "keycloak"


class OrganizationsInputError(ValueError):
    """Standard input is not a JSON object of tenant name to organization alias."""


class OrganizationLinkConflict(RuntimeError):
    """The canonical store holds a link the seed would have to take over from someone else."""


class UsersInputError(ValueError):
    """Standard input is not a JSON object of username to Keycloak account."""


class UserLinkConflict(RuntimeError):
    """The canonical store holds a user the seed would have to take over from someone else."""


@dataclass(frozen=True)
class KeycloakAccount:
    """A realm user the seed links to a canonical user."""

    username: str
    external_sub: str
    email: str
    given_name: str
    family_name: str


_ACCOUNT_FIELDS = ("external_sub", "email", "given_name", "family_name")


def parse_organizations(raw: object) -> dict[str, str]:
    """A non-empty JSON object of tenant name to non-empty organization alias, else ``OrganizationsInputError``."""
    if not isinstance(raw, dict) or not raw:
        raise OrganizationsInputError("the organizations must be a non-empty JSON object of tenant name to alias")
    for name, alias in raw.items():
        if not isinstance(alias, str) or not alias:
            raise OrganizationsInputError(f"the alias of tenant {name!r} must be a non-empty string")
    return dict(raw)


def parse_users(raw: object) -> list[KeycloakAccount]:
    """A non-empty JSON object of username to account, each field a non-empty string, else ``UsersInputError``."""
    if not isinstance(raw, dict) or not raw:
        raise UsersInputError("the users must be a non-empty JSON object of username to account")
    accounts = []
    for username, account in sorted(raw.items()):
        if not isinstance(account, dict) or set(account) != set(_ACCOUNT_FIELDS):
            raise UsersInputError(f"user {username!r} must be an object of exactly {', '.join(_ACCOUNT_FIELDS)}")
        for field in _ACCOUNT_FIELDS:
            if not isinstance(account[field], str) or not account[field]:
                raise UsersInputError(f"{field} of user {username!r} must be a non-empty string")
        accounts.append(KeycloakAccount(username=username, **account))
    for field in ("external_sub", "email"):
        values = [getattr(account, field) for account in accounts]
        if len(set(values)) != len(values):
            raise UsersInputError(f"two users name the same {field}")
    return accounts


def linked_user(account: KeycloakAccount, link: IdPLink | None, user: User | None) -> User:
    """The canonical user ``account`` stands for.

    ``user`` is the user ``link`` names when the account is linked, else the
    user holding the account's email, if any. A linked user is returned as it
    is, whatever its status: the status is Keycloak's to say, through the event
    pull and reconciliation. With no link, a new user is returned; a user that
    holds the email without the link is refused (``UserLinkConflict``): an email
    match proves nothing about who holds the row.
    """
    if link is not None:
        if user is None or user.id != link.user_id:
            raise UserLinkConflict(f"the link of Keycloak user {account.external_sub} names no user")
        return user
    if user is not None:
        raise UserLinkConflict(
            f"a user {account.email!r} exists that is not linked to Keycloak user {account.external_sub}"
        )
    return User(
        email=account.email,
        user_name=account.username,
        given_name=account.given_name,
        family_name=account.family_name,
        status=UserStatus.active,
    )


async def link_users(*, provider_id: uuid_mod.UUID, accounts: list[KeycloakAccount], dry_run: bool) -> None:
    """Link each account to a canonical user in one transaction: a refusal, or a dry run, writes nothing.

    The users are written as Keycloak holds them, so they are the provider's
    state (``provider_writes``): nothing is marked for a reconciliation pass to
    push back.
    """
    async with get_session_factory()() as session:
        await PendingPushRepository(session).provider_writes(True)
        for account in accounts:
            link_query = select(IdPLink).where(
                IdPLink.provider_id == provider_id, IdPLink.external_sub == account.external_sub
            )
            link = (await session.execute(link_query)).scalars().first()
            holder = User.id == link.user_id if link is not None else User.email == account.email
            user = (await session.execute(select(User).where(holder))).scalars().first()
            canonical = linked_user(account, link, user)
            if link is not None:
                print(f"  [skip] user {account.username!r} is already linked")
                continue
            session.add(canonical)
            await session.flush()
            session.add(
                IdPLink(
                    user_id=canonical.id,
                    provider_id=provider_id,
                    external_sub=account.external_sub,
                    external_email=account.email,
                    metadata_={LINK_ORIGIN: LINKED_BY_BACKEND},
                )
            )
            done = "dry-run" if dry_run else "linked"
            print(f"  [{done}] user {account.username!r} to Keycloak user {account.external_sub}")
        if dry_run:
            await session.rollback()
        else:
            await session.commit()


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
        await link_organizations_in(session, provider_id=provider_id, organizations=organizations, dry_run=dry_run)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()


async def link_organizations_in(
    session: AsyncSession, *, provider_id: uuid_mod.UUID, organizations: dict[str, str], dry_run: bool = False
) -> None:
    """Link each tenant to its organization alias in ``session``, which the caller commits or rolls back.

    The organizations exist in Keycloak already, so the links are the provider's
    state (``provider_writes``): nothing is marked for a reconciliation pass to
    push, which would overwrite the organization with the tenant, and the next
    pass takes the organization's name, domains and status into the tenant.
    """
    await PendingPushRepository(session).provider_writes(True)
    for name, alias in sorted(organizations.items()):
        # Already linked: a reconciliation pass may have renamed it after its organization since.
        linked = (
            (
                await session.execute(
                    select(Tenant).where(
                        Tenant.external_org_id == alias, Tenant.external_org_provider_id == provider_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if linked is not None:
            link_tenant(linked, linked.name, alias, provider_id)
            print(f"  [skip] tenant {linked.name!r} is already linked to organization {alias!r}")
            continue
        holders = (
            await session.execute(select(Tenant.name).where(Tenant.external_org_id == alias, Tenant.name != name))
        ).scalars()
        holder = holders.first()
        if holder is not None:
            raise OrganizationLinkConflict(f"organization {alias!r} is already linked to tenant {holder!r}")
        tenant = (await session.execute(select(Tenant).where(Tenant.name == name))).scalars().first()
        session.add(link_tenant(tenant, name, alias, provider_id))
        await session.flush()
        print(f"  [{'dry-run' if dry_run else 'linked'}] tenant {name!r} to organization {alias!r}")


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
    links = parser.add_mutually_exclusive_group()
    links.add_argument(
        "--link-organizations",
        action="store_true",
        help="Also link tenants to organizations, from a JSON object of tenant name to alias on standard input",
    )
    links.add_argument(
        "--link-users",
        action="store_true",
        help="Also link canonical users to Keycloak users, from a JSON object of username to account on standard input",
    )
    args = parser.parse_args()
    organizations: dict[str, str] = {}
    accounts: list[KeycloakAccount] = []
    try:
        if args.link_organizations:
            organizations = parse_organizations(json.load(sys.stdin))
        if args.link_users:
            accounts = parse_users(json.load(sys.stdin))
    except (json.JSONDecodeError, OrganizationsInputError, UsersInputError) as exc:
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

    if accounts and provider_id is None:
        print(
            "2. [dry-run] Would link users to Keycloak users once the provider exists:", [a.username for a in accounts]
        )
    elif accounts and provider_id is not None:
        print("2. Linking users to Keycloak users...")
        try:
            await link_users(provider_id=provider_id, accounts=accounts, dry_run=args.dry_run)
        except UserLinkConflict as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
