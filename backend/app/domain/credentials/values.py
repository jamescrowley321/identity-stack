"""Entities and value objects of the Credentials context."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

# Longest lifetime a credential may be given, in seconds: 365 days, the longest
# option the SPA offers.
MAX_LIFETIME_SECONDS = 365 * 24 * 60 * 60


# Roles no machine credential may hold. ``operator`` opens the state of every
# tenant (``require_global_role``) to a caller whose sole membership grants it,
# and a credential always has exactly one membership: holding it there would
# give the credential what its creator, a member of several tenants, is refused.
ROLES_NOT_FOR_MACHINES = frozenset({"operator"})


class CredentialStatus(enum.StrEnum):
    """Whether a credential may sign in. The names are the ones the API reports."""

    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class MachineCredential:
    """A credential a machine signs in with, owned by one tenant.

    ``client_id`` is what the machine authenticates as, ``subject`` the ``sub`` of
    the tokens it is issued, and ``external_id`` the provider's own id for it.
    ``role_names`` are the roles it holds in its tenant.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    provider: str
    name: str
    client_id: str
    subject: str
    external_id: str
    status: CredentialStatus
    expires_at: datetime
    created_at: datetime
    role_names: tuple[str, ...]
