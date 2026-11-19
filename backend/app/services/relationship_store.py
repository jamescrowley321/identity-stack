"""The relationship store a deployment checks fine-grained relations in.

Only the kind for now: ``GET /api/capabilities`` reports it, and the FGA and
document routes answer 404 without a store (``app.dependencies.capabilities``).
"""

from __future__ import annotations

import enum


class RelationshipStoreKind(enum.StrEnum):
    # Descope's FGA store, keyed on Descope tenant and user ids.
    DESCOPE = "descope"
    # No store: the FGA and document routes are not served.
    NONE = "none"
