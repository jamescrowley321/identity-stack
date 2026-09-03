import os

from fastapi import APIRouter, Depends, Request

from app.dependencies.auth import get_claims
from app.middleware.providers import build_provider_configs
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.services.logout import LOGOUT_STRATEGIES

router = APIRouter(tags=["Auth"])


def _configured_providers():
    """Build the configured provider list from env — mirrors middleware/factory.py."""
    ory_require_audience = os.getenv("ORY_REQUIRE_AUDIENCE", "true").strip().lower() not in ("false", "0", "no")
    return build_provider_configs(
        descope_project_id=os.getenv("DESCOPE_PROJECT_ID", ""),
        ory_issuer_url=os.getenv("ORY_ISSUER_URL", ""),
        ory_audience=os.getenv("ORY_AUDIENCE") or None,
        ory_require_audience=ory_require_audience,
    )


@router.post("/auth/logout")
@limiter.limit(RATE_LIMIT_AUTH)
async def logout(request: Request, claims: dict = Depends(get_claims)):
    """Log out the current user via the strategy for their auth provider.

    The provider is selected from the authenticated principal — never hardcoded
    (NFR-5). Descope revokes sessions server-side and returns ``logged_out``
    (unchanged, NFR-8); Ory (and any RP-initiated OIDC provider) returns a
    ``logout_url`` the SPA redirects the browser to. An unrecognized/absent
    provider falls back to the first configured provider (Descope) so the shared
    entrypoint never crashes an authenticated request.
    """
    principal = getattr(request.state, "principal", None)
    provider_name = ""
    if principal is not None and principal.identity is not None:
        provider_name = (principal.identity.authentication_type or "").lower()

    providers = _configured_providers()
    provider = next((p for p in providers if p.name.lower() == provider_name), providers[0])

    # RP-initiated logout needs the ID token as id_token_hint; the SPA sends it
    # in the JSON body. Descope sends no body — tolerate an absent/invalid body.
    id_token = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            id_token = body.get("id_token")
    except Exception:
        id_token = None

    strategy = LOGOUT_STRATEGIES[provider.logout_kind]
    return await strategy(provider, claims, id_token)
