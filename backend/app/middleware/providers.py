"""Provider-agnostic token-validation configuration.

Generalizes the previously Descope-hardcoded issuer allow-list, discovery
address, audience, and claim handling so any configured OIDC provider can be
validated by the same middleware. Descope stays a configured provider — adding
Ory (or any OIDC provider) is configuration, not new validation code.

Both TokenValidationMiddleware (standalone: verifies the JWT signature via
py-identity-model) and GatewayClaimsMiddleware (gateway: decodes the
Tyk-pre-validated payload) build the same provider list and select a provider by
the token's ``iss``. The Descope entry reproduces the historical behavior
exactly, including the no-project-id path where the issuer allow-list is empty
and issuer validation is skipped.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderTokenConfig:
    """How to validate and interpret tokens from one OIDC provider."""

    name: str
    accepted_issuers: frozenset[str]
    disco_address: str
    audience: str | None = None
    infer_single_tenant_dct: bool = False


def descope_config(project_id: str) -> ProviderTokenConfig:
    """Descope provider config.

    Descope emits two issuer formats — OIDC/ID tokens
    (``https://api.descope.com/{pid}``) and session/access-key tokens
    (``https://api.descope.com/v1/apps/{pid}``) — both signed by the same JWKS.
    With no project id the allow-list is empty (issuer validation skipped), which
    preserves the historical test path.
    """
    accepted = (
        frozenset(
            {
                f"https://api.descope.com/{project_id}",
                f"https://api.descope.com/v1/apps/{project_id}",
            }
        )
        if project_id
        else frozenset()
    )
    return ProviderTokenConfig(
        name="Descope",
        accepted_issuers=accepted,
        disco_address=f"https://api.descope.com/{project_id}/.well-known/openid-configuration",
        audience=project_id or None,
        infer_single_tenant_dct=True,
    )


def ory_config(issuer_url: str, audience: str | None = None) -> ProviderTokenConfig:
    """Ory Network provider config — standard OIDC.

    Ory issues a single OIDC-compliant issuer and JWT access tokens validated via
    py-identity-model (the path py-identity-model already runs against this exact
    Ory project). It does not use Descope's ``dct``/``tenants`` claims — tenant and
    roles are resolved from the canonical model, not the token.
    """
    issuer = issuer_url.rstrip("/")
    return ProviderTokenConfig(
        name="Ory",
        accepted_issuers=frozenset({issuer}),
        disco_address=f"{issuer}/.well-known/openid-configuration",
        audience=audience or None,
        infer_single_tenant_dct=False,
    )


def build_provider_configs(
    *,
    descope_project_id: str = "",
    ory_issuer_url: str = "",
    ory_audience: str | None = None,
) -> list[ProviderTokenConfig]:
    """Build the configured provider list.

    Descope is always present (preserving single-provider behavior, including the
    no-project-id path). Ory is appended when an issuer is configured.
    """
    providers = [descope_config(descope_project_id)]
    if ory_issuer_url:
        providers.append(ory_config(ory_issuer_url, ory_audience))
    return providers


def unverified_issuer(token: str) -> str | None:
    """Best-effort read of ``iss`` without verifying the signature.

    Used ONLY to order which provider's discovery/JWKS to try first — never as a
    security decision (the signature is always verified against the selected
    provider's JWKS). Returns None for opaque or mock tokens.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    iss = claims.get("iss") if isinstance(claims, dict) else None
    return iss if isinstance(iss, str) else None


def order_candidates(providers: list[ProviderTokenConfig], iss_hint: str | None) -> list[ProviderTokenConfig]:
    """Order providers to try for signature validation (standalone path).

    - With a decodable ``iss`` hint: try the provider whose allow-list contains it.
      If it matches none and some provider enforces an allow-list, return [] so the
      token is rejected without a crypto attempt; if no provider enforces one
      (legacy no-config), fall back to all.
    - Without a hint (opaque/mock token): try issuer-configured providers first so
      a mock token is attributed by its returned ``iss``, with empty-allow-list
      providers last (legacy catch-all).
    """
    if iss_hint is not None:
        matched = [p for p in providers if iss_hint in p.accepted_issuers]
        if matched:
            return matched
        if any(p.accepted_issuers for p in providers):
            return []
        return list(providers)
    configured = [p for p in providers if p.accepted_issuers]
    unconfigured = [p for p in providers if not p.accepted_issuers]
    return configured + unconfigured


def select_by_issuer(providers: list[ProviderTokenConfig], iss: object) -> ProviderTokenConfig | None:
    """Select a provider by an already-decoded ``iss`` (gateway path).

    If no provider enforces an issuer allow-list (all empty), returns the first
    provider so issuer validation is skipped — the historical no-project-id
    behavior. Otherwise returns the provider whose allow-list contains ``iss``, or
    None when a config exists but ``iss`` matches none (reject).
    """
    if not any(p.accepted_issuers for p in providers):
        return providers[0] if providers else None
    if not isinstance(iss, str):
        return None
    for provider in providers:
        if iss in provider.accepted_issuers:
            return provider
    return None


def audience_ok(aud: object, expected: str) -> bool:
    """True if the token audience matches (string equality or membership in a list)."""
    return aud == expected or (isinstance(aud, list) and expected in aud)


def infer_single_tenant_dct(claims: dict, provider: ProviderTokenConfig) -> None:
    """Descope-only: when ``dct`` is absent but exactly one tenant exists, infer it.

    No-op for providers (e.g. Ory) that don't carry Descope's ``tenants`` claim.
    """
    if not provider.infer_single_tenant_dct:
        return
    if not claims.get("dct") and isinstance(claims.get("tenants"), dict):
        tenants = claims["tenants"]
        if len(tenants) == 1:
            claims["dct"] = next(iter(tenants))
