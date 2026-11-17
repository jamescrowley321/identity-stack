"""Link the local Zitadel instance to canonical tenants.

Reads the ``canonical_seed`` output of the ``infra/zitadel`` Terraform root on
standard input and writes, idempotently:

- canonical tenants ``acme`` and ``globex``, each with ``external_org_id`` set to
  its Zitadel organization id;
- a ``providers`` row ``zitadel`` of type ``oidc`` on the instance's issuer;
- the ``acme`` test user, linked (``idp_links``) on its Zitadel user id, with
  the global canonical roles the output names: ``admin`` in ``acme`` and
  ``viewer`` in ``globex``.

A second run with the same output changes nothing. The seed takes over nothing
it did not write: it refuses, writing nothing, when ``acme`` or ``globex`` is
already linked to another organization (``external_org_id`` is shared with the
other providers), or when a user with the test user's email exists without a
link to its Zitadel user. After ``make zitadel-reset`` and a new ``terraform
apply`` the ids are new, so the seed needs a database it has not seeded. The
``globex`` test user is left for first login to provision, and the third
organization gets no tenant.

Canonical roles are matched by name among the global roles (``tenant_id`` is
NULL) and created, without permissions, only when missing.

Run it as a module from ``backend/``, so ``app`` is importable:

    terraform -chdir=infra/zitadel output -json canonical_seed \\
        | (cd backend && DATABASE_URL=postgresql+asyncpg://... python -m scripts.seed_zitadel)

``make zitadel-seed`` does the same.
"""

import asyncio
import json
import sys
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.database import get_session_factory
from app.models.identity.assignment import UserTenantRole
from app.models.identity.provider import Provider, ProviderType
from app.models.identity.role import Role
from app.models.identity.tenant import Tenant, TenantStatus
from app.models.identity.user import IdPLink, User, UserStatus

PROVIDER_NAME = "zitadel"
CANONICAL_ROLES = frozenset({"owner", "admin", "member", "viewer"})


class SeedInputError(ValueError):
    """The Terraform output is not a complete, consistent ``canonical_seed``."""


class SeedConflictError(RuntimeError):
    """The canonical store holds a row the seed would have to take over from someone else."""


@dataclass(frozen=True)
class LinkedUser:
    external_sub: str
    email: str
    user_name: str
    given_name: str
    family_name: str
    # Canonical tenant name -> canonical role name.
    roles: dict[str, str]


@dataclass(frozen=True)
class SeedInput:
    issuer_url: str
    # Canonical tenant name -> Zitadel organization id.
    tenants: dict[str, str]
    linked_user: LinkedUser


def _string(data: dict, key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SeedInputError(f"{where}.{key} must be a non-empty string")
    return value


def _string_map(data: dict, key: str, where: str) -> dict[str, str]:
    value = data.get(key)
    if not isinstance(value, dict) or not value:
        raise SeedInputError(f"{where}.{key} must be a non-empty object")
    for name, item in value.items():
        if not isinstance(item, str) or not item:
            raise SeedInputError(f"{where}.{key}.{name} must be a non-empty string")
    return dict(value)


def parse_seed_input(raw: object) -> SeedInput:
    """Read the ``canonical_seed`` Terraform output, refusing anything incomplete or inconsistent."""
    if not isinstance(raw, dict):
        raise SeedInputError("canonical_seed must be an object")
    tenants = _string_map(raw, "tenants", "canonical_seed")
    user = raw.get("linked_user")
    if not isinstance(user, dict):
        raise SeedInputError("canonical_seed.linked_user must be an object")
    where = "canonical_seed.linked_user"
    roles = _string_map(user, "roles", where)
    for tenant, role in roles.items():
        if tenant not in tenants:
            raise SeedInputError(f"{where}.roles names tenant {tenant!r}, which canonical_seed.tenants does not")
        if role not in CANONICAL_ROLES:
            raise SeedInputError(f"{where}.roles.{tenant} is {role!r}, not one of {sorted(CANONICAL_ROLES)}")
    return SeedInput(
        issuer_url=_string(raw, "issuer_url", "canonical_seed"),
        tenants=tenants,
        linked_user=LinkedUser(
            external_sub=_string(user, "external_sub", where),
            email=_string(user, "email", where),
            user_name=_string(user, "user_name", where),
            given_name=_string(user, "given_name", where),
            family_name=_string(user, "family_name", where),
            roles=roles,
        ),
    )


def zitadel_provider(*, issuer_url: str) -> Provider:
    """The ``zitadel`` provider row: type ``oidc``, the instance's issuer, single sign-on only."""
    issuer = issuer_url.rstrip("/")
    return Provider(
        name=PROVIDER_NAME,
        type=ProviderType.oidc,
        issuer_url=issuer,
        base_url=issuer,
        capabilities=["sso"],
    )


async def _ensure_provider(session: AsyncSession, seed: SeedInput, changes: list[str]) -> Provider:
    wanted = zitadel_provider(issuer_url=seed.issuer_url)
    provider = (await session.execute(select(Provider).where(Provider.name == PROVIDER_NAME))).scalars().first()
    if provider is None:
        session.add(wanted)
        await session.flush()
        changes.append(f"created provider {PROVIDER_NAME!r} (issuer {wanted.issuer_url})")
        return wanted
    fields = ("type", "issuer_url", "base_url", "capabilities", "active")
    current = {field: getattr(provider, field) for field in fields}
    target = {field: getattr(wanted, field) for field in fields}
    if current != target:
        for field, value in target.items():
            setattr(provider, field, value)
        changes.append(f"updated provider {PROVIDER_NAME!r} from {current} to {target}")
    return provider


async def _ensure_tenants(session: AsyncSession, seed: SeedInput, changes: list[str]) -> dict[str, Tenant]:
    tenants: dict[str, Tenant] = {}
    for name, org_id in sorted(seed.tenants.items()):
        tenant = (await session.execute(select(Tenant).where(Tenant.name == name))).scalars().first()
        if tenant is None:
            tenant = Tenant(name=name, external_org_id=org_id, status=TenantStatus.active)
            session.add(tenant)
            changes.append(f"created tenant {name!r} (organization {org_id})")
        elif tenant.external_org_id is None:
            tenant.external_org_id = org_id
            changes.append(f"linked tenant {name!r} to organization {org_id}")
        elif tenant.external_org_id != org_id:
            # external_org_id is shared with the other providers' organizations
            # (migration 004): another id here is not the seed's to move.
            raise SeedConflictError(
                f"tenant {name!r} is already linked to organization {tenant.external_org_id}, not {org_id}"
            )
        tenants[name] = tenant
    await session.flush()
    return tenants


async def _ensure_roles(session: AsyncSession, names: set[str], changes: list[str]) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for name in sorted(names):
        query = select(Role).where(Role.name == name, Role.tenant_id.is_(None))
        role = (await session.execute(query)).scalars().first()
        if role is None:
            role = Role(name=name)
            session.add(role)
            changes.append(f"created global role {name!r}")
        roles[name] = role
    await session.flush()
    return roles


async def _ensure_user(session: AsyncSession, seed: SeedInput, provider: Provider, changes: list[str]) -> User:
    wanted = seed.linked_user
    link_query = select(IdPLink).where(IdPLink.provider_id == provider.id, IdPLink.external_sub == wanted.external_sub)
    link = (await session.execute(link_query)).scalars().first()
    if link is not None:
        return (await session.execute(select(User).where(User.id == link.user_id))).scalars().one()

    # An email match proves nothing about who holds the row: linking it would hand
    # its account, and the grants below, to the Zitadel user.
    if (await session.execute(select(User).where(User.email == wanted.email))).scalars().first() is not None:
        raise SeedConflictError(
            f"a user {wanted.email!r} exists that is not linked to Zitadel user {wanted.external_sub}"
        )

    user = User(
        email=wanted.email,
        user_name=wanted.user_name,
        given_name=wanted.given_name,
        family_name=wanted.family_name,
        status=UserStatus.active,
    )
    session.add(user)
    await session.flush()
    session.add(
        IdPLink(user_id=user.id, provider_id=provider.id, external_sub=wanted.external_sub, external_email=wanted.email)
    )
    await session.flush()
    changes.append(f"created user {wanted.email!r}, linked to Zitadel user {wanted.external_sub}")
    return user


async def _ensure_assignments(
    session: AsyncSession,
    seed: SeedInput,
    user: User,
    tenants: dict[str, Tenant],
    roles: dict[str, Role],
    changes: list[str],
) -> None:
    for tenant_name, role_name in sorted(seed.linked_user.roles.items()):
        tenant, role = tenants[tenant_name], roles[role_name]
        query = select(UserTenantRole).where(
            UserTenantRole.user_id == user.id,
            UserTenantRole.tenant_id == tenant.id,
            UserTenantRole.role_id == role.id,
        )
        if (await session.execute(query)).scalars().first() is None:
            session.add(UserTenantRole(user_id=user.id, tenant_id=tenant.id, role_id=role.id))
            changes.append(f"assigned {seed.linked_user.email!r} role {role_name!r} in tenant {tenant_name!r}")
    await session.flush()


async def apply_seed(session: AsyncSession, seed: SeedInput) -> list[str]:
    """Write the seed in ``session`` and commit. Returns what changed; an empty list on a rerun.

    Raises ``SeedConflictError``, having committed nothing, when the store holds a
    tenant or user the seed would have to take over; the caller rolls back.
    """
    changes: list[str] = []
    provider = await _ensure_provider(session, seed, changes)
    tenants = await _ensure_tenants(session, seed, changes)
    roles = await _ensure_roles(session, set(seed.linked_user.roles.values()), changes)
    user = await _ensure_user(session, seed, provider, changes)
    await _ensure_assignments(session, seed, user, tenants, roles, changes)
    await session.commit()
    return changes


async def main() -> int:
    try:
        seed = parse_seed_input(json.load(sys.stdin))
    except (json.JSONDecodeError, SeedInputError) as exc:
        print(f"ERROR: standard input is not the canonical_seed output of infra/zitadel: {exc}", file=sys.stderr)
        return 1

    async with get_session_factory()() as session:
        try:
            changes = await apply_seed(session, seed)
        except SeedConflictError as exc:
            await session.rollback()
            print(f"ERROR: nothing written: {exc}. Seed a database that has no such row.", file=sys.stderr)
            return 1

    for change in changes:
        print(f"  [changed] {change}")
    if not changes:
        print("  [unchanged] the canonical store already matches the instance")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
