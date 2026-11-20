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
class CapabilityMissing(NotFound):
    """The deployment lacks the capability a route serves (``app.dependencies.capabilities``).

    404, not the provider's 403: no principal is refused, the route is not there.
    """


@dataclass(frozen=True, unsafe_hash=False)
class Conflict(IdentityError):
    """Operation conflicts with existing state (e.g. duplicate name)."""


@dataclass(frozen=True, unsafe_hash=False)
class EmailLinkRefused(Conflict):
    """A first login's email is already held by a canonical user it may not be linked to.

    Just-in-time provisioning of a provider whose organization picks the tenant
    (Zitadel) returns it: an organization's administrators can set any email on
    their users, so the email does not show the login is that user's. Any first
    login (just-in-time provisioning, the Descope flow sync) returns it too for
    a user such a login created (``User.email_from_organization``).
    """


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


@dataclass(frozen=True, unsafe_hash=False)
class GrantRefused(Forbidden):
    """The caller asked to grant something it does not hold itself (a machine credential's roles).

    403 like ``Forbidden``, but its message is returned as it is: it names only the
    roles the caller asked for.
    """


@dataclass(frozen=True, unsafe_hash=False)
class MachinePrincipalRefused(Forbidden):
    """A machine credential's token on a route that only a person may call (``/api/keys``).

    403 like ``Forbidden``; its message names only the rule, so it is returned as it is.
    """


class IdentityErrorRaised(Exception):
    """Raise a domain ``IdentityError`` from a place that cannot return a Result.

    Services return ``Result`` and never raise — that contract is unchanged. This
    exists for *guard* helpers in the routing layer, where returning a value the
    caller has to remember to check is fail-open by construction: a guard whose
    result is dropped silently stops guarding, with no type error and no test
    failure. Raising removes the thing that can be forgotten.

    ``app.errors.problem_detail.identity_error_raised_handler`` turns it into the
    same RFC 9457 Problem Detail that ``result_to_response`` produces, so the
    response shape is identical whether the error was returned or raised.
    """

    def __init__(self, error: IdentityError) -> None:
        self.error = error
        super().__init__(error.message)
