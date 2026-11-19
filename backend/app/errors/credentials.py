"""Translate Credentials domain errors into the IdentityError a service returns."""

from __future__ import annotations

from app.domain import credentials
from app.errors.identity import GrantRefused, IdentityError, NotFound, ValidationError


def identity_error(error: credentials.CredentialError) -> IdentityError:
    match error:
        case credentials.ExpiryMissing():
            return ValidationError(
                message=(
                    "expire_time is required and must be a future epoch-seconds timestamp; "
                    "a key that never expires outlives the membership that created it"
                )
            )
        case credentials.ExpiryNotInFuture():
            return ValidationError(message="expire_time must be in the future")
        case credentials.ExpiryTooFar(max_days=max_days):
            return ValidationError(message=f"expire_time may be at most {max_days} days in the future")
        case credentials.RolesNotHeld(roles=roles):
            return GrantRefused(message=f"Cannot grant roles you do not hold: {', '.join(roles)}")
        case credentials.RolesMissing():
            return ValidationError(message="role_names must name at least one role the credential holds")
        case credentials.RolesNotForMachines(roles=roles):
            return GrantRefused(message=f"A machine credential cannot hold: {', '.join(roles)}")
        case credentials.RolesNotFound(roles=roles):
            return ValidationError(message=f"The tenant has no roles named: {', '.join(roles)}")
        case credentials.CredentialNotFound():
            # One answer for a credential that is not there and one of another tenant.
            return NotFound(message="Machine credential not found")
    raise TypeError(f"no IdentityError for {type(error).__name__}")
