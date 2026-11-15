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

from app.knobs import knob, knob_flag

RS256 = "RS256"
# The claim that names the client a token was issued to (OIDC Core §2).
AUTHORIZED_PARTY_CLAIM = "azp"


class ProviderConfigurationError(ValueError):
    """A provider is configured in a way that would accept tokens it must not."""


@dataclass(frozen=True)
class ProviderTokenConfig:
    """How to validate and interpret tokens from one OIDC provider."""

    name: str
    accepted_issuers: frozenset[str]
    disco_address: str
    audience: str | None = None
    infer_single_tenant_dct: bool = False
    # When True, a token MUST carry an ``aud`` that matches ``audience`` (fail
    # closed). When False, ``aud`` is only checked if present — the Descope
    # behavior, since Descope session/access-key tokens omit ``aud``.
    require_audience: bool = False
    # The provider's non-URL issuer form, if it has one — Descope's bare project
    # id. Tokens on this issuer carry no ``aud``, so the audience check above
    # cannot bind them to this API; ``token_kind_rejected`` binds them by kind
    # instead. ``None`` leaves the kind unchecked.
    bare_issuer: str | None = None
    # ``drn`` values acceptable on ``bare_issuer``. Descope stamps ``DS`` on a
    # session token. Verified against the live project: a session JWT carries
    # ``{drn, exp, iat, iss, rexp, sub, tenants}`` — ``drn`` is the only claim
    # that names what kind of token it is.
    session_token_kinds: frozenset[str] = frozenset({"DS"})
    # How this provider logs a user out (see app.services.logout). "management"
    # is Descope's server-side session revocation (the historical default);
    # "rp_initiated" is standard OIDC RP-initiated logout (Ory) — redirect the
    # browser to the provider's ``end_session_endpoint``.
    logout_kind: str = "management"
    # Several acceptable audiences (Keycloak: the type client ids). When set it
    # is used instead of ``audience``: the token's ``aud`` must name one of them.
    audiences: frozenset[str] = frozenset()
    # Signing algorithms accepted from this provider. Empty accepts whichever
    # algorithm the provider's JWKS key declares.
    algorithms: frozenset[str] = frozenset()
    # Require HTTPS for discovery, JWKS and the logout discovery fetch. The
    # library still allows plain HTTP on loopback when this is on.
    require_https: bool = True
    # RP-initiated logout: the client id is fixed (``logout_client_id``), or read
    # from a claim of the caller's token (``logout_client_id_claim``), or else
    # ``audience`` (Ory with no ``ORY_CLIENT_ID``).
    logout_client_id: str | None = None
    logout_client_id_claim: str | None = None
    post_logout_redirect_uri: str | None = None


def descope_config(project_id: str) -> ProviderTokenConfig:
    """Descope provider config.

    Descope emits three issuer formats, all signed by the same project JWKS:

    - ``https://api.descope.com/{pid}`` — OIDC / ID tokens
    - ``https://api.descope.com/v1/apps/{pid}`` — OIDC inbound-app tokens
    - ``{pid}`` (the **bare project id**) — SDK / session / access-key tokens,
      e.g. the ``sessionJwt`` returned by the access-key exchange endpoint

    All three are accepted; the signature is always verified against the project
    JWKS, so the issuer allow-list is not the security boundary. Omitting the
    bare-``{pid}`` form rejected valid access-key session tokens outright in
    ``order_candidates`` (before any crypto), 401ing the admin E2E path. With no
    project id the allow-list is empty (issuer validation skipped), which
    preserves the historical test path.
    """
    accepted = (
        frozenset(
            {
                project_id,
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
        bare_issuer=project_id or None,
    )


def ory_config(
    issuer_url: str,
    audience: str | None = None,
    require_audience: bool = True,
    *,
    client_id: str | None = None,
    post_logout_redirect_uri: str | None = None,
) -> ProviderTokenConfig:
    """Ory Network provider config — standard OIDC.

    Ory issues a single OIDC-compliant issuer and JWT access tokens validated via
    py-identity-model (the path py-identity-model already runs against this exact
    Ory project). It does not use Descope's ``dct``/``tenants`` claims — tenant and
    roles are resolved from the canonical model, not the token.

    Audience is enforced fail-closed by default (``require_audience=True``): without
    it, any token the Ory project issues — for any client/audience, or with no
    ``aud`` at all — would authenticate (confused-deputy).
    """
    issuer = issuer_url.rstrip("/")
    return ProviderTokenConfig(
        name="Ory",
        accepted_issuers=frozenset({issuer}),
        disco_address=f"{issuer}/.well-known/openid-configuration",
        audience=audience or None,
        infer_single_tenant_dct=False,
        require_audience=require_audience,
        logout_kind="rp_initiated",
        logout_client_id=client_id or None,
        post_logout_redirect_uri=post_logout_redirect_uri,
    )


def keycloak_config(
    issuer_url: str,
    audiences: frozenset[str],
    *,
    require_https: bool = True,
    post_logout_redirect_uri: str | None = None,
) -> ProviderTokenConfig:
    """Keycloak realm provider config.

    ``issuer_url`` is ``<KC_HOSTNAME>/realms/<realm>``, the exact ``iss`` of the
    realm's tokens. ``audiences`` are the type client ids: ``aud`` names the type
    and ``azp`` the app client, so logout takes its client id from ``azp``.

    Fails closed: ``aud`` is always required, only RS256 is accepted, and a
    config with no audience is refused, because it would accept every token the
    realm issues for any client.
    """
    issuer = issuer_url.rstrip("/")
    accepted_audiences = frozenset(audience.strip() for audience in audiences if audience.strip())
    if not accepted_audiences:
        raise ProviderConfigurationError(
            "KEYCLOAK_AUDIENCES must name at least one type client id when KEYCLOAK_ISSUER_URL is set"
        )
    return ProviderTokenConfig(
        name="Keycloak",
        accepted_issuers=frozenset({issuer}),
        disco_address=f"{issuer}/.well-known/openid-configuration",
        audiences=accepted_audiences,
        require_audience=True,
        algorithms=frozenset({RS256}),
        require_https=require_https,
        logout_kind="rp_initiated",
        logout_client_id_claim=AUTHORIZED_PARTY_CLAIM,
        post_logout_redirect_uri=post_logout_redirect_uri,
    )


def build_provider_configs(
    *,
    descope_project_id: str = "",
    ory_issuer_url: str = "",
    ory_audience: str | None = None,
    ory_require_audience: bool = True,
    ory_client_id: str | None = None,
    ory_post_logout_redirect_uri: str | None = None,
    keycloak_issuer_url: str = "",
    keycloak_audiences: frozenset[str] = frozenset(),
    keycloak_require_https: bool = True,
    keycloak_post_logout_redirect_uri: str | None = None,
) -> list[ProviderTokenConfig]:
    """Build the provider list from explicit values. ``configured_providers`` is the
    caller that reads them from the environment.

    Descope is always present (preserving single-provider behavior, including the
    no-project-id path). Ory and Keycloak are each appended when an issuer is
    configured. A Keycloak issuer with no audience raises (see ``keycloak_config``).

    Fail-closed: if Ory is configured with audience enforcement on (the default) but
    no ``ory_audience`` is provided, raise at startup rather than silently accept any
    Ory-issued token. Set ``ory_require_audience=False`` (env ``ORY_REQUIRE_AUDIENCE=false``)
    to knowingly opt out.
    """
    providers = [descope_config(descope_project_id)]
    if ory_issuer_url:
        if ory_require_audience and not ory_audience:
            raise ValueError(
                "ORY_AUDIENCE must be set when ORY_ISSUER_URL is set — Ory access-token "
                "audience validation is required. Set ORY_REQUIRE_AUDIENCE=false to opt out."
            )
        providers.append(
            ory_config(
                ory_issuer_url,
                ory_audience,
                require_audience=ory_require_audience,
                client_id=ory_client_id,
                post_logout_redirect_uri=ory_post_logout_redirect_uri,
            )
        )
    if keycloak_issuer_url:
        providers.append(
            keycloak_config(
                keycloak_issuer_url,
                keycloak_audiences,
                require_https=keycloak_require_https,
                post_logout_redirect_uri=keycloak_post_logout_redirect_uri,
            )
        )
    return providers


def configured_providers() -> list[ProviderTokenConfig]:
    """The provider list this deployment's environment configures.

    The one builder behind the middleware factory and the logout router. It reads
    the environment when called (conventions §1): the factory calls it once at
    startup, the logout router on each request. A provider configured so that it
    would accept tokens it must not raises here, which stops startup.
    """
    frontend_url = knob("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    default_post_logout_redirect_uri = f"{frontend_url}/login"
    # Client ids are case-sensitive, so this list is not read with ``knob_set``,
    # which lowercases its entries.
    keycloak_audiences = frozenset(knob("KEYCLOAK_AUDIENCES", "").split(","))
    return build_provider_configs(
        descope_project_id=knob("DESCOPE_PROJECT_ID", ""),
        ory_issuer_url=knob("ORY_ISSUER_URL", ""),
        ory_audience=knob("ORY_AUDIENCE") or None,
        ory_require_audience=knob_flag("ORY_REQUIRE_AUDIENCE", "true"),
        ory_client_id=knob("ORY_CLIENT_ID") or None,
        ory_post_logout_redirect_uri=knob("ORY_POST_LOGOUT_REDIRECT_URI") or default_post_logout_redirect_uri,
        keycloak_issuer_url=knob("KEYCLOAK_ISSUER_URL", ""),
        keycloak_audiences=keycloak_audiences,
        keycloak_require_https=knob_flag("KEYCLOAK_REQUIRE_HTTPS", "true"),
        keycloak_post_logout_redirect_uri=(
            knob("KEYCLOAK_POST_LOGOUT_REDIRECT_URI") or default_post_logout_redirect_uri
        ),
    )


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


def accepted_audiences(provider: ProviderTokenConfig) -> frozenset[str]:
    """The audiences this provider's tokens may carry: ``audiences``, else the single ``audience``."""
    if provider.audiences:
        return provider.audiences
    return frozenset({provider.audience}) if provider.audience else frozenset()


def _names_an_accepted_audience(aud: object, accepted: frozenset[str]) -> bool:
    """True if ``aud`` (a string, or a list of them) names at least one accepted audience."""
    if isinstance(aud, str):
        return aud in accepted
    if isinstance(aud, list):
        return any(isinstance(entry, str) and entry in accepted for entry in aud)
    return False


def audience_rejected(claims: dict, provider: ProviderTokenConfig) -> bool:
    """True if the token must be rejected on audience grounds for this provider.

    - ``require_audience`` (Ory, Keycloak): ``aud`` MUST be present and name an
      accepted audience — fail closed, also when no audience is configured.
    - otherwise (Descope): ``aud`` is checked only when present (session tokens omit it).
    """
    accepted = accepted_audiences(provider)
    if provider.require_audience:
        return "aud" not in claims or not _names_an_accepted_audience(claims["aud"], accepted)
    return bool(accepted) and "aud" in claims and not _names_an_accepted_audience(claims["aud"], accepted)


def algorithm_rejected(token: str, provider: ProviderTokenConfig) -> bool:
    """True if the token's signing algorithm is not one this provider accepts.

    Reads ``alg`` from the JWT header. The header is covered by the signature the
    standalone middleware goes on to verify, so a token only authenticates when
    its real algorithm is accepted. Enforced here because py-identity-model, in
    discovery mode, replaces ``TokenValidationConfig.algorithms`` with the
    algorithm of the JWKS key the token selects.

    Fails closed: with algorithms pinned, a header that cannot be read is rejected.
    """
    if not provider.algorithms:
        return False
    try:
        header_b64 = token.split(".")[0]
        header_b64 += "=" * (-len(header_b64) % 4)
        header = json.loads(base64.urlsafe_b64decode(header_b64))
    except Exception:
        return True
    alg = header.get("alg") if isinstance(header, dict) else None
    return not isinstance(alg, str) or alg not in provider.algorithms


def token_kind_rejected(claims: dict, provider: ProviderTokenConfig) -> bool:
    """True if the token must be rejected because of what KIND of token it is.

    Only the bare-project-id issuer is checked, and only when the provider names
    one. That issuer is the one form Descope uses for the tokens its SDK mints,
    and those tokens carry no ``aud`` — so ``audience_rejected`` cannot bind them
    to this API, and every credential the project signs would otherwise
    authenticate here. Descope's refresh token is signed by the same project
    JWKS, carries the same bare issuer, omits ``aud`` in the same way, and is
    built to live in a browser far longer than the 180-second session token;
    ``drn`` is the only claim that separates them.

    Fails closed: a bare-issuer token with no ``drn`` at all is rejected, because
    Descope stamps one on every token it mints on that issuer. URL-issuer tokens
    (OIDC / inbound-app) are untouched — they are bound by ``aud`` instead.
    """
    if not provider.bare_issuer or claims.get("iss") != provider.bare_issuer:
        return False
    return claims.get("drn") not in provider.session_token_kinds


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
