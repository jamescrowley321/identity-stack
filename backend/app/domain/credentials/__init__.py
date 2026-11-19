"""Credentials: the credentials a machine signs in with, each owned by one tenant.

Other layers import this package, not its modules. The glossary is in
docs/adr/0008-layered-architecture-with-a-functional-domain-core.md.
"""

from app.domain.credentials.decisions import expiry, grantable_roles, in_force, known_roles
from app.domain.credentials.errors import (
    CredentialError,
    CredentialNotFound,
    ExpiryMissing,
    ExpiryNotInFuture,
    ExpiryTooFar,
    RolesNotFound,
    RolesNotHeld,
)
from app.domain.credentials.values import MAX_LIFETIME_SECONDS, CredentialStatus, MachineCredential

__all__ = [
    "MAX_LIFETIME_SECONDS",
    "CredentialError",
    "CredentialNotFound",
    "CredentialStatus",
    "ExpiryMissing",
    "ExpiryNotInFuture",
    "ExpiryTooFar",
    "MachineCredential",
    "RolesNotFound",
    "RolesNotHeld",
    "expiry",
    "grantable_roles",
    "in_force",
    "known_roles",
]
