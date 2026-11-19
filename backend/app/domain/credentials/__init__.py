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

__all__ = [
    "MAX_LIFETIME_SECONDS",
    "ROLES_NOT_FOR_MACHINES",
    "CredentialError",
    "CredentialNotFound",
    "CredentialStatus",
    "ExpiryMissing",
    "ExpiryNotInFuture",
    "ExpiryTooFar",
    "MachineCredential",
    "RolesMissing",
    "RolesNotForMachines",
    "RolesNotFound",
    "RolesNotHeld",
    "expiry",
    "grantable_roles",
    "in_force",
    "known_roles",
]
