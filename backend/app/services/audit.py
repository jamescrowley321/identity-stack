"""Audit logging for security-sensitive operations.

Emits structured audit events to a dedicated ``audit`` logger. In production
(JSON logging), each event is a single JSON object containing who, what, when,
where, and result — suitable for ingestion by SIEM/log aggregation systems.

Phase 1 stores events in structured logs only. A future phase can persist to a
database table or external audit service.
"""

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field
from starlette.requests import Request

from app.logging_config import get_logger

audit_logger = get_logger("audit")


class AuditEventType(StrEnum):
    USER_LOGIN = "user_login"
    USER_LOGOUT = "user_logout"
    ROLE_ASSIGNED = "role_assigned"
    ROLE_REMOVED = "role_removed"
    ACCESS_KEY_CREATED = "access_key_created"
    ACCESS_KEY_DEACTIVATED = "access_key_deactivated"
    ACCESS_KEY_ACTIVATED = "access_key_activated"
    ACCESS_KEY_DELETED = "access_key_deleted"
    TENANT_CREATED = "tenant_created"
    USER_INVITED = "user_invited"
    USER_DEACTIVATED = "user_deactivated"
    USER_ACTIVATED = "user_activated"
    USER_REMOVED = "user_removed"
    PROFILE_UPDATED = "profile_updated"
    TENANT_SETTINGS_UPDATED = "tenant_settings_updated"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditEvent(BaseModel):
    """A single audit log entry capturing a security-sensitive operation."""

    timestamp: str = Field(default_factory=_utc_now_iso)
    action: AuditEventType
    actor_id: str | None = None
    tenant_id: str | None = None
    target: dict = Field(default_factory=dict)
    ip_address: str | None = None
    result: str = "success"
    detail: str = ""


def _get_client_ip(request: Request) -> str | None:
    """Extract the client IP, preferring X-Forwarded-For for proxied requests."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # First entry is the original client
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def emit_audit_event(event: AuditEvent) -> None:
    """Write an audit event to the audit logger as a structured log entry."""
    audit_logger.info(
        "audit.%s",
        event.action,
        extra={"audit_event": event.model_dump()},
    )


def audit_event(
    request: Request,
    action: AuditEventType,
    target: dict,
    *,
    result: str = "success",
    detail: str = "",
) -> None:
    """Convenience function to emit an audit event from a request context.

    Extracts actor_id, tenant_id, and IP address from the request automatically.
    """
    claims = getattr(request.state, "claims", None) or {}
    event = AuditEvent(
        action=action,
        actor_id=claims.get("sub"),
        tenant_id=claims.get("dct"),
        target=target,
        ip_address=_get_client_ip(request),
        result=result,
        detail=detail,
    )
    emit_audit_event(event)
