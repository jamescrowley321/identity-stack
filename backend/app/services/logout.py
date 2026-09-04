"""Provider-aware logout strategies.

Each configured OIDC provider ends a session differently:

- **Descope** (``logout_kind="management"``) revokes all of the user's sessions
  server-side via the management API. The backend performs the call and returns
  ``{"status": "logged_out", ...}`` — the historical behavior, preserved
  byte-for-byte (NFR-8).
- **Standard OIDC providers such as Ory** (``logout_kind="rp_initiated"``) use
  OIDC RP-initiated logout: the backend cannot end the session itself, so it
  returns the provider's ``end_session_endpoint`` URL (with ``id_token_hint`` and
  a registered ``post_logout_redirect_uri``) as ``{"status": "logout_redirect",
  "logout_url": ...}`` and the SPA redirects the browser to it.

``routers/auth.py`` selects a strategy by the caller's provider via
``LOGOUT_STRATEGIES`` — no provider is hardcoded on the logout entrypoint
(NFR-5). Reuses py-identity-model's discovery + end-session-URL helpers rather
than adding new OIDC logic (NFR-4).
"""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException
from py_identity_model import DiscoveryDocumentRequest, build_end_session_url
from py_identity_model.aio import get_discovery_document

from app.middleware.providers import ProviderTokenConfig

DESCOPE_BASE_URL = os.getenv("DESCOPE_BASE_URL", "https://api.descope.com")


async def _descope_management_logout(provider: ProviderTokenConfig, claims: dict, id_token: str | None) -> dict:
    """Revoke all the user's Descope sessions via the management API.

    Unchanged from the pre-ORY-5.1 behavior: same endpoint, headers, body, and
    response shape (NFR-8). ``id_token`` is unused — Descope revokes server-side.
    """
    project_id = os.getenv("DESCOPE_PROJECT_ID", "")
    management_key = os.getenv("DESCOPE_MANAGEMENT_KEY", "")
    user_id = claims.get("sub")

    if user_id and management_key:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{DESCOPE_BASE_URL}/v1/mgmt/user/logout",
                headers={"Authorization": f"Bearer {project_id}:{management_key}"},
                json={"userId": user_id},
            )

    return {"status": "logged_out", "sub": user_id}


def _default_post_logout_redirect_uri() -> str:
    """The URL the provider redirects back to after logout.

    Defaults to the SPA's ``/login`` (derived from ``FRONTEND_URL``). This URI
    MUST be registered in the Ory OAuth2 client's ``post_logout_redirect_uris``
    (Epic 1) or the provider rejects the redirect.
    """
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return os.getenv("ORY_POST_LOGOUT_REDIRECT_URI") or f"{frontend_url}/login"


async def _ory_rp_initiated_logout(provider: ProviderTokenConfig, claims: dict, id_token: str | None) -> dict:
    """Build an OIDC RP-initiated logout URL for a standard provider (Ory).

    Fail-closed: if discovery fails or the provider advertises no
    ``end_session_endpoint`` we raise 500 rather than return 200 with no URL,
    which would silently leave the provider session live.
    """
    disco = await get_discovery_document(DiscoveryDocumentRequest(address=provider.disco_address))
    # ``is_successful`` is unguarded, but ``end_session_endpoint`` is a guarded
    # field that raises FailedResponseAccessError on a failed discovery (and
    # getattr's default does NOT suppress it), so check success before reading it.
    end_session_endpoint = disco.end_session_endpoint if getattr(disco, "is_successful", False) else None
    if not end_session_endpoint:
        raise HTTPException(
            status_code=500,
            detail="Provider does not advertise an end_session_endpoint for RP-initiated logout",
        )

    logout_url = build_end_session_url(
        end_session_endpoint,
        id_token_hint=id_token,
        client_id=os.getenv("ORY_CLIENT_ID") or provider.audience,
        post_logout_redirect_uri=_default_post_logout_redirect_uri(),
    )
    return {"status": "logout_redirect", "logout_url": logout_url, "sub": claims.get("sub")}


# Dispatch table keyed by ProviderTokenConfig.logout_kind. Adding a provider is
# configuration (a new logout_kind row), not a change to the logout entrypoint.
LOGOUT_STRATEGIES = {
    "management": _descope_management_logout,
    "rp_initiated": _ory_rp_initiated_logout,
}
