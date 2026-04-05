"""Identity domain error hierarchy.

All service methods return Result[T, IdentityError] — never raise for domain errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, unsafe_hash=False)
class IdentityError:
    """Base class for all identity domain errors.

    Note: instances with a non-None ``context`` dict are not hashable because
    ``dict`` is mutable.  ``unsafe_hash=False`` is set explicitly so that
    Python raises ``TypeError`` immediately rather than producing an
    inconsistent hash.
    """

    message: str
    context: dict[str, Any] | None = field(default=None)


@dataclass(frozen=True, unsafe_hash=False)
class NotFound(IdentityError):
    """Requested resource does not exist."""


@dataclass(frozen=True, unsafe_hash=False)
class Conflict(IdentityError):
    """Operation conflicts with existing state (e.g. duplicate name)."""


@dataclass(frozen=True, unsafe_hash=False)
class ValidationError(IdentityError):
    """Input failed domain validation rules."""


@dataclass(frozen=True, unsafe_hash=False)
class SyncFailed(IdentityError):
    """Postgres write succeeded but IdP sync failed — maps to HTTP 207 (Multi-Status).

    The local write succeeded but the upstream provider sync did not.  HTTP 207
    signals partial success so callers know to retry the sync.
    """

    operation: str = ""
    payload_summary: str = ""
    underlying_error: str = ""


@dataclass(frozen=True, unsafe_hash=False)
class ProviderError(IdentityError):
    """Upstream identity provider returned an error."""


@dataclass(frozen=True, unsafe_hash=False)
class Forbidden(IdentityError):
    """Caller lacks permission for the requested operation."""
