"""Decisions of the Credentials context: what a new credential may be, and whether one is in force."""

from __future__ import annotations

from datetime import datetime

from expression import Error, Ok, Result

from app.domain.credentials.errors import (
    CredentialError,
    ExpiryMissing,
    ExpiryNotInFuture,
    ExpiryTooFar,
    RolesMissing,
    RolesNotForMachines,
    RolesNotFound,
    RolesNotHeld,
)
from app.domain.credentials.values import (
    MAX_LIFETIME_SECONDS,
    ROLES_NOT_FOR_MACHINES,
    CredentialStatus,
    MachineCredential,
)


def expiry(expire_time: int | None, *, now: int) -> Result[int, CredentialError]:
    """A bounded, future expiry in epoch seconds.

    0 and an absent value are refused: Descope reads both as "never expires", and a
    credential that never expires outlives the membership of whoever created it.
    """
    if expire_time is None or expire_time == 0:
        return Error(ExpiryMissing())
    if expire_time <= now:
        return Error(ExpiryNotInFuture())
    if expire_time > now + MAX_LIFETIME_SECONDS:
        return Error(ExpiryTooFar(max_days=MAX_LIFETIME_SECONDS // 86400))
    return Ok(expire_time)


def holdable_roles(role_names: tuple[str, ...]) -> Result[tuple[str, ...], CredentialError]:
    """Roles a machine credential may hold at all: at least one, none of ``ROLES_NOT_FOR_MACHINES``.

    Checked when a credential is created and again on every request it makes: a role
    renamed or deleted since (inbound sync, the role routes) changes what it holds.
    """
    if not role_names:
        return Error(RolesMissing())
    refused = tuple(sorted(set(role_names) & ROLES_NOT_FOR_MACHINES))
    if refused:
        return Error(RolesNotForMachines(roles=refused))
    return Ok(tuple(dict.fromkeys(role_names)))


def grantable_roles(requested: tuple[str, ...], *, held: frozenset[str]) -> Result[tuple[str, ...], CredentialError]:
    """The roles a new credential may carry: at least one, none of ``ROLES_NOT_FOR_MACHINES``, and only
    roles its creator holds in the tenant.

    There is no role hierarchy, and authorization gates on role names, so "a lesser
    role" cannot be decided: containment is the rule, and it covers ``owner``.
    """
    holdable = holdable_roles(requested)
    if holdable.is_error():
        return holdable
    escalated = tuple(sorted(set(requested) - held))
    if escalated:
        return Error(RolesNotHeld(roles=escalated))
    # Each role once: the credential's role links are one per role.
    return Ok(holdable.ok)


def known_roles(requested: tuple[str, ...], *, found: frozenset[str]) -> Result[tuple[str, ...], CredentialError]:
    """The requested roles, when the tenant has every one of them."""
    missing = tuple(sorted(set(requested) - found))
    if missing:
        return Error(RolesNotFound(roles=missing))
    return Ok(requested)


def in_force(credential: MachineCredential, *, now: datetime) -> bool:
    """Whether the credential's tokens are accepted: active and not expired."""
    return credential.status is CredentialStatus.ACTIVE and credential.expires_at > now
