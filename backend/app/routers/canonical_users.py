"""Canonical users router — list/filter from the canonical Postgres table.

Distinct from `users.py` which exposes Descope-managed tenant members.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.dependencies.identity import get_user_service
from app.dependencies.rbac import require_role
from app.models.identity.user import UserStatus
from app.services.user import UserService

router = APIRouter(tags=["Canonical Users"])

_MAX_USERS_LIMIT = 200

# `provisional` is the spec/UX term; `provisioned` is the canonical enum.
_STATUS_ALIASES = {"provisional": UserStatus.provisioned}


def _parse_status(value: str | None) -> UserStatus | None:
    if value is None or value == "":
        return None
    if value in _STATUS_ALIASES:
        return _STATUS_ALIASES[value]
    try:
        return UserStatus(value)
    except ValueError as exc:
        allowed = ", ".join(v.value for v in UserStatus) + ", provisional"
        raise HTTPException(status_code=422, detail=f"Invalid status '{value}'. Allowed: {allowed}") from exc


@router.get("/users")
async def list_canonical_users(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=_MAX_USERS_LIMIT),
    _operator_roles: list[str] = Depends(require_role("operator")),
    service: UserService = Depends(get_user_service),
):
    """List canonical users, optionally filtered by status."""
    status_enum = _parse_status(status)
    users = await service.list_canonical_users(status=status_enum, limit=limit)
    return {
        "users": [
            {
                "id": str(u.id),
                "email": u.email,
                "user_name": u.user_name,
                "given_name": u.given_name,
                "family_name": u.family_name,
                "status": u.status.value,
                "created_at": u.created_at.isoformat(),
                "updated_at": u.updated_at.isoformat(),
            }
            for u in users
        ]
    }
