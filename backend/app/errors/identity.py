"""Identity domain error hierarchy.

All service methods return Result[T, IdentityError] — never raise for domain errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IdentityError:
    """Base class for all identity domain errors."""

    message: str
    context: dict | None = field(default=None)


@dataclass(frozen=True)
class NotFound(IdentityError):
    """Requested resource does not exist."""


@dataclass(frozen=True)
class Conflict(IdentityError):
    """Operation conflicts with existing state (e.g. duplicate name)."""


@dataclass(frozen=True)
class ValidationError(IdentityError):
    """Input failed domain validation rules."""


@dataclass(frozen=True)
class SyncFailed(IdentityError):
    """Postgres write succeeded but IdP sync failed — maps to HTTP 202."""

    operation: str = ""
    payload_summary: str = ""
    underlying_error: str = ""


@dataclass(frozen=True)
class ProviderError(IdentityError):
    """Upstream identity provider returned an error."""


@dataclass(frozen=True)
class Forbidden(IdentityError):
    """Caller lacks permission for the requested operation."""
