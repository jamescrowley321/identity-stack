"""Expected failures of the Credentials context, as values."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialError:
    """Base of every expected failure in the Credentials context."""


@dataclass(frozen=True)
class ExpiryMissing(CredentialError):
    """No expiry, or 0: a credential that never expires outlives the membership that created it."""


@dataclass(frozen=True)
class ExpiryNotInFuture(CredentialError):
    """The expiry is now or in the past."""


@dataclass(frozen=True)
class ExpiryTooFar(CredentialError):
    """The expiry is further ahead than a credential may live."""

    max_days: int


@dataclass(frozen=True)
class RolesNotHeld(CredentialError):
    """The credential would carry roles its creator does not hold in the tenant."""

    roles: tuple[str, ...]


@dataclass(frozen=True)
class RolesNotFound(CredentialError):
    """Roles the tenant has none of: neither its own nor global."""

    roles: tuple[str, ...]


@dataclass(frozen=True)
class CredentialNotFound(CredentialError):
    """No credential of that id in the caller's tenant."""

    credential_id: str
