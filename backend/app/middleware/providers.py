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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.knobs import knob, knob_flag

RS256 = "RS256"
# The claim that names the client a token was issued to (OIDC Core §2).
AUTHORIZED_PARTY_CLAIM = "azp"


class ProviderConfigurationError(ValueError):
    """A provider is configured in a way that would accept tokens it must not."""


@dataclass(frozen=True)
class TenantClaims:
    """What one token says about its caller's tenant.

    ``tenant_id`` is the tenant the token is for, or ``None`` when it names none.
    ``roles`` and ``permissions`` are what the token grants in that tenant.
    """

    tenant_id: str | None
    roles: list[str]
    permissions: list[str]


# A provider's claims extractor: it reads the tenant and the grants out of the
# claims of a token this provider issued.
TenantClaimsExtractor = Callable[[dict], TenantClaims]


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
    # Reads the tenant and the grants from this provider's token claims
    # (conventions §18). ``None``: the token carries neither, and they come from
    # the canonical model when the provider is in ``CANONICAL_RBAC_PROVIDERS``.
    tenant_claims: TenantClaimsExtractor | None = None
    # True when this provider's tenant ids and subjects are Descope's own, so its
    # principals may use the routes that hand them to Descope's Management API
    # and FGA store (``app.dependencies.descope.require_descope_principal``).
    descope_managed: bool = False


def _string_list(value: object) -> list[str]:
    """The strings in ``value`` when it is a list, else nothing.

    A grant list of the wrong type grants nothing, and an entry that is not a
    string is dropped: the strings beside it are still granted, as they always were.
    """
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]


def descope_tenant_claims(claims: dict) -> TenantClaims:
    """Descope's extractor: ``dct`` is the current tenant, ``tenants[dct]`` holds its grants.

    Fails closed on a malformed token. A ``dct`` that is not a non-empty string
    names no tenant, and a ``tenants`` entry of the wrong shape grants nothing.
    """
    tenant_id = claims.get("dct")
    if not isinstance(tenant_id, str) or not tenant_id:
        return TenantClaims(tenant_id=None, roles=[], permissions=[])
    tenants = claims.get("tenants")
    tenant_info = tenants.get(tenant_id) if isinstance(tenants, dict) else None
    if not isinstance(tenant_info, dict):
        return TenantClaims(tenant_id=tenant_id, roles=[], permissions=[])
    return TenantClaims(
        tenant_id=tenant_id,
        roles=_string_list(tenant_info.get("roles")),
        permissions=_string_list(tenant_info.get("permissions")),
    )


def claimed_tenant_id(claims: dict, provider: ProviderTokenConfig) -> str | None:
    """The tenant ``provider``'s extractor reads from ``claims``, or ``None`` with no extractor.

    Also ``None`` when the extractor answers with anything but a non-empty
    string. An extractor that raises is not caught here: both middlewares call
    this while authenticating, where every failure is the uniform 401.
    """
    if provider.tenant_claims is None:
        return None
    tenant_id = provider.tenant_claims(claims).tenant_id
    return tenant_id if isinstance(tenant_id, str) and tenant_id else None


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
        tenant_claims=descope_tenant_claims,
        descope_managed=True,
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


def _ory_provider(
    issuer_url: str,
    audience: str | None,
    *,
    require_audience: bool,
    client_id: str | None,
    post_logout_redirect_uri: str | None,
) -> ProviderTokenConfig | None:
    """Ory when it has an issuer, refusing audience enforcement with no audience to enforce.

    Fail-closed: with enforcement on (the default) and no audience, every token
    would be rejected at request time, and with it off every Ory-issued token
    would be accepted. Raising here stops startup instead.
    """
    if not issuer_url:
        return None
    if require_audience and not audience:
        raise ValueError(
            "ORY_AUDIENCE must be set when ORY_ISSUER_URL is set — Ory access-token "
            "audience validation is required. Set ORY_REQUIRE_AUDIENCE=false to opt out."
        )
    return ory_config(
        issuer_url,
        audience,
        require_audience=require_audience,
        client_id=client_id,
        post_logout_redirect_uri=post_logout_redirect_uri,
    )


def _keycloak_provider(
    issuer_url: str,
    audiences: frozenset[str],
    *,
    require_https: bool,
    post_logout_redirect_uri: str | None,
) -> ProviderTokenConfig | None:
    """Keycloak when it has an issuer. An issuer with no audience raises (see ``keycloak_config``)."""
    if not issuer_url:
        return None
    return keycloak_config(
        issuer_url,
        audiences,
        require_https=require_https,
        post_logout_redirect_uri=post_logout_redirect_uri,
    )


def _default_post_logout_redirect_uri() -> str:
    """Where a provider sends the browser after logout unless it names its own: the SPA's login page."""
    frontend_url = knob("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{frontend_url}/login"


def _descope_registration() -> ProviderTokenConfig:
    """Descope is always registered, also with no project id (the historical single-provider path)."""
    return descope_config(knob("DESCOPE_PROJECT_ID", ""))


def _ory_registration() -> ProviderTokenConfig | None:
    """Ory, when ``ORY_ISSUER_URL`` is set."""
    return _ory_provider(
        knob("ORY_ISSUER_URL", ""),
        knob("ORY_AUDIENCE") or None,
        require_audience=knob_flag("ORY_REQUIRE_AUDIENCE", "true"),
        client_id=knob("ORY_CLIENT_ID") or None,
        post_logout_redirect_uri=knob("ORY_POST_LOGOUT_REDIRECT_URI") or _default_post_logout_redirect_uri(),
    )


def _keycloak_registration() -> ProviderTokenConfig | None:
    """Keycloak, when ``KEYCLOAK_ISSUER_URL`` is set."""
    return _keycloak_provider(
        knob("KEYCLOAK_ISSUER_URL", ""),
        # Client ids are case-sensitive, so this list is not read with ``knob_set``,
        # which lowercases its entries.
        frozenset(knob("KEYCLOAK_AUDIENCES", "").split(",")),
        require_https=knob_flag("KEYCLOAK_REQUIRE_HTTPS", "true"),
        post_logout_redirect_uri=(knob("KEYCLOAK_POST_LOGOUT_REDIRECT_URI") or _default_post_logout_redirect_uri()),
    )


# One entry per provider this backend can be configured with. An entry reads its
# own settings from the environment and returns the provider, or ``None`` when
# the deployment does not configure it. Adding a provider is adding its entry
# (and its claims extractor, if its tokens carry a tenant): nothing in the
# middleware factory, the middlewares, the logout router or the logout service
# names a provider.
ProviderRegistration = Callable[[], ProviderTokenConfig | None]
PROVIDER_REGISTRATIONS: list[ProviderRegistration] = [
    _descope_registration,
    _ory_registration,
    _keycloak_registration,
]


def _issuer_key(issuer: str) -> str:
    """``issuer`` as two spellings of one issuer compare equal.

    Trailing slashes are dropped and the scheme and host are case-folded. The
    path keeps its case (two realms may differ only in that), and so does an
    issuer that is not a URL, such as Descope's bare project id.
    """
    trimmed = issuer.rstrip("/")
    parts = urlsplit(trimmed)
    if not parts.scheme or not parts.netloc:
        return trimmed
    return urlunsplit(parts._replace(scheme=parts.scheme.casefold(), netloc=parts.netloc.casefold()))


def _refuse_ambiguous_providers(providers: list[ProviderTokenConfig]) -> None:
    """Raise when two providers could be taken for each other.

    The dependencies and the logout router find a request's provider by name
    (compared case-insensitively) and the middlewares by issuer, so a shared
    name or issuer would apply one provider's rules to the other's tokens. Two
    issuers that differ only in a trailing slash or in the case of the scheme or
    host count as shared: a token is matched by the exact string, but no
    deployment means them as two.
    """
    names: set[str] = set()
    issuers: set[str] = set()
    for provider in providers:
        name = provider.name.lower()
        if name in names:
            raise ProviderConfigurationError(f"provider {provider.name!r} is registered more than once")
        names.add(name)
        shared = sorted(issuer for issuer in provider.accepted_issuers if _issuer_key(issuer) in issuers)
        if shared:
            raise ProviderConfigurationError(
                f"provider {provider.name!r} accepts an issuer another provider already accepts: {shared}"
            )
        issuers |= {_issuer_key(issuer) for issuer in provider.accepted_issuers}


def refuse_canonical_descope_managed(providers: Iterable[ProviderTokenConfig], canonical: Iterable[str]) -> None:
    """Raise when a ``descope_managed`` provider is also authorized from the canonical model.

    ``canonical`` is ``CANONICAL_RBAC_PROVIDERS``, lower-case provider names. A
    provider in it gets its tenant from the canonical model, and a
    ``descope_managed`` provider passes ``require_descope_principal``: with
    both, a canonical tenant id would be handed to Descope, where it names
    nothing or someone else's tenant. The middleware factory calls this at
    startup.
    """
    canonical_names = {name.lower() for name in canonical}
    both = sorted(p.name for p in providers if p.descope_managed and p.name.lower() in canonical_names)
    if both:
        raise ProviderConfigurationError(
            f"CANONICAL_RBAC_PROVIDERS must not list {both}: the tenant and user ids of a provider "
            "whose routes call Descope come from its own token claims"
        )


def _assemble(registrations: Iterable[ProviderRegistration]) -> list[ProviderTokenConfig]:
    """Walk ``registrations`` into a provider list, refusing an ambiguous one.

    The one assembly behind ``configured_providers`` and ``build_provider_configs``.
    """
    providers = [provider for registration in registrations if (provider := registration()) is not None]
    _refuse_ambiguous_providers(providers)
    return providers


def configured_providers() -> list[ProviderTokenConfig]:
    """The provider list this deployment's environment configures.

    The one builder behind the middleware factory and the logout router: it walks
    ``PROVIDER_REGISTRATIONS`` and takes no per-provider argument. It reads the
    environment when called (conventions §1): the factory calls it once at
    startup, the logout router on each request. A provider configured so that it
    would accept tokens it must not raises here, which stops startup.
    """
    return _assemble(PROVIDER_REGISTRATIONS)


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
    """``configured_providers`` over explicit values, for a middleware built by hand.

    The application does not call this. It is the same assembly as
    ``configured_providers`` with the shipped registrations' settings passed in
    instead of read from the environment: each provider is built by the function
    its registration calls, and an ambiguous list is refused the same way.

    Descope is always present (preserving single-provider behavior, including the
    no-project-id path). Ory and Keycloak are each added when an issuer is given.
    A Keycloak issuer with no audience raises (see ``keycloak_config``), and so
    does an Ory issuer with audience enforcement on (the default) and no
    ``ory_audience``. Pass ``ory_require_audience=False`` to knowingly opt out.
    """
    return _assemble(
        [
            lambda: descope_config(descope_project_id),
            lambda: _ory_provider(
                ory_issuer_url,
                ory_audience,
                require_audience=ory_require_audience,
                client_id=ory_client_id,
                post_logout_redirect_uri=ory_post_logout_redirect_uri,
            ),
            lambda: _keycloak_provider(
                keycloak_issuer_url,
                keycloak_audiences,
                require_https=keycloak_require_https,
                post_logout_redirect_uri=keycloak_post_logout_redirect_uri,
            ),
        ]
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
    """True if the token's header names a signing algorithm this provider does not accept.

    Gateway mode only. There the gateway verifies the signature and this backend
    never calls py-identity-model, so the header is the only place to apply the
    provider's ``algorithms``. In standalone mode the library enforces the same
    set through ``TokenValidationConfig.algorithms``.

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
