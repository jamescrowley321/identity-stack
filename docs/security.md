# Security

This documents the application-level security controls. For vulnerability reporting see [`../SECURITY.md`](../SECURITY.md).

## Token validation

Bearer tokens are validated by [`py-identity-model`](https://github.com/jamescrowley321/identity-model) (the 5.0.0 pre-release, pinned to one commit of its repository; it enforces a provider's signing-algorithm allowlist and pulls `cryptography>=50` with the PYSEC-2026-3552/3553/3554 fixes). Validation accepts all three Descope issuer formats, all signed by the same project JWKS. The library checks a token's `iss` against the three (`TokenValidationConfig.token_issuers`, set by `library_token_issuers()` in `middleware/providers.py`) and uses them only with the keys of a discovery document whose own issuer is one of them; every other provider's tokens must carry its discovery issuer:

- `https://api.descope.com/{project_id}` — OIDC / ID tokens
- `https://api.descope.com/v1/apps/{project_id}` — OIDC inbound-app tokens
- `{project_id}` — the **bare project id**, used by SDK / session / access-key tokens (e.g. the `sessionJwt` from the access-key exchange endpoint)

When `ORY_ISSUER_URL` is set, Ory access tokens are accepted too: one issuer, with the audience enforced unless `ORY_REQUIRE_AUDIENCE` is off (`middleware/providers.py`).

In `gateway` mode, Tyk verifies the token signature at the edge. The backend's `GatewayClaimsMiddleware` decodes the forwarded JWT without re-checking the signature, and still checks expiry, issuer, signing algorithm, audience and sender constraint as defence in depth (`middleware/factory.py`, `middleware/claims.py`).

### Signing algorithms

Every provider accepts RS256 only (`ProviderTokenConfig.algorithms` in `middleware/providers.py`). A token signed with any other algorithm is answered 401, even when its key is published in the provider's own JWKS and the signature would verify. `POST /api/validate-id-token`, which runs behind neither middleware, validates with the Descope provider's config and answers a refused token with the same 401. When the token cannot be judged at all, because the provider's discovery document or JWKS cannot be fetched or is unusable, or the provider config is refused (plain HTTP where HTTPS is required), it answers 503 `{"detail": "Identity provider unavailable"}` and logs the cause at ERROR. Its log lines name the error type only, never the library's message, which quotes the token's header.

| Provider | Accepted | Basis |
|---|---|---|
| Descope | RS256 | The project's JWKS publishes one RSA key with `alg: RS256`, and its discovery document advertises RS256 only. |
| Ory | RS256 | Ory Network and a default Hydra sign access tokens with RS256 keys. |
| Keycloak | RS256 | Project policy for the realm. |

In `standalone` mode the library enforces the set on the token's header `alg` (RFC 8725 §3.1). In `gateway` mode the backend never calls the library, so `GatewayClaimsMiddleware` applies the same set to the header itself.

No environment variable changes the set. An Ory Network project or a Hydra deployment configured to sign access tokens with another algorithm (ES256, for example) has every token refused with 401 until that algorithm is added to the set in `ory_config()`; a Descope project's set is in `descope_config()` and a Keycloak realm's in `keycloak_config()`, all in `middleware/providers.py`. In `gateway` mode the gateway has to verify signatures of that algorithm as well.

Independently of the set, the library accepts no HMAC algorithm and no unsigned token.

### Sender-constrained tokens

The backend accepts bearer tokens only. An access token bound to a DPoP key (`cnf.jkt`, RFC 9449) or to a client certificate (`cnf["x5t#S256"]`, RFC 8705) is valid only with its proof or its certificate, and the backend checks neither, so a bound token is answered 401 in both modes. In `standalone` mode the library refuses it (RFC 9449 §7.2). In `gateway` mode the gateway's JWT check does not read `cnf`, so `GatewayClaimsMiddleware` refuses it (`sender_constrained()` in `middleware/providers.py`).

## Authorization (RBAC + FGA)

Route handlers enforce authorization with dependency guards built over the validated claims:

- `require_role(...)` — role checks against the token's `tenants`/`dct` claims, or against the canonical model for providers in `CANONICAL_RBAC_PROVIDERS` (`dependencies/rbac.py`).
- `require_permission(...)` — permission checks, resolved the same way.

A Keycloak principal is decided from its token alone: the tenant is the one organization the token names and the permissions are the client roles of its one audience. The token is first held to one `aud` and the `azp` configured for it, at authentication and on every route. A user type whose roles would otherwise carry across organizations is either limited to listed read-only permissions or accepted only for a user in exactly one organization. A token whose organization or client roles cannot be read names no tenant and is refused everywhere. A token of an organization-bound type is confirmed by the realm's Admin REST API on every request, through a service account that holds only the `realm-management` roles `view-users` and `view-organizations` and is refused every write. The member it asks about is the verified token's `sub`, never a request value, and a `sub` that is not a well-formed id is refused before any call. Belonging to one organization does not show that a role was granted for it: Keycloak keeps a user's client roles when the user leaves an organization. So the organization must also mark the user as holding the type (its `type:<type client>` attribute lists the user's id), which only a realm admin can write. A directory that cannot be asked is 502 and no grant; its HTTP status is logged, the service account's secret never is. See [architecture.md](architecture.md#keycloak-token-model) and [conventions §17](conventions.md#17-authorization-fails-closed).

Keycloak principals are not provisioned just in time by default. When an operator adds `keycloak` to `JIT_ENABLED_PROVIDERS` (the compose setup in [local-development.md](local-development.md#backend-configuration) does), a Keycloak principal's first `GET /api/identity` provisions a canonical user, as an Ory principal's does. Provisioning links the new identity to an existing canonical user whose email matches only when the token says `email_verified`, so the realm's `email_verified` is trusted across providers: whoever controls a verified email in the realm is linked to the canonical user of any provider with that email. A realm used here must keep that claim meaningful. It must not broker another identity provider whose email it takes on trust (an identity provider with "Trust email" on), must not let users register themselves, must keep emails unique, and must verify an email before it counts, so an email a user changes is not `email_verified` until they prove it (`verifyEmail` on). The compose realm does all four, and `tests/integration/test_keycloak_identity.py::test_the_realm_keeps_email_verified_meaningful_for_cross_provider_linking` reads them back from it.

`KEYCLOAK_REQUIRE_HTTPS=false` is accepted only for an issuer on `localhost`, a loopback address or a `.localhost` name. The check reads the name and does not resolve it. A `.localhost` name is reserved for loopback, but a resolver or hosts file that points one elsewhere is not detected, and the discovery document and signing keys would then be fetched from that host over plain HTTP. Leave HTTPS enforcement on outside local development and tests. The backend logs a warning at startup whenever it is off.

Both, and `get_tenant_id`, take the tenant and the grants from one resolver (`dependencies/tenant_access.py`, [conventions §18](conventions.md#18-one-resolver-decides-the-tenant-and-the-grants)). A principal from a provider in `CANONICAL_RBAC_PROVIDERS` who belongs to several tenants names one with the `X-Tenant-ID` request header. The header only chooses among the caller's own canonical memberships: a tenant outside them is answered with 403, and a Descope principal's tenant is always its `dct` claim, whatever the header says. The routes that hand the tenant or the caller's `sub` to Descope's Management API or FGA store (documents, members, access keys, FGA relations, the current tenant and its settings, `GET /api/roles/me`, and `POST /api/roles/assign` and `/api/roles/remove`, whose assignments are mirrored to Descope) serve Descope principals only, and so do the routes over state that belongs to the whole Descope project (the role and permission definitions and `GET` and `PUT /api/fga/schema`), which a canonical principal could otherwise reach as an admin of any one of their tenants: `require_descope_principal` (`dependencies/descope.py`) answers a principal of any other provider with 403 before Descope is called. The routes over state that belongs to no tenant and is not Descope's (`GET /api/users`, which lists every tenant's users, `/api/providers`, `/api/sync/status` and `/api/events/recent`) are gated by `require_global_role("operator")`. The request chooses nothing there: a canonical principal is served on the role of their sole membership, and one who belongs to several tenants is refused, whether they name one with the header, carry a `dct` claim on the token, or name none, so being an operator of one tenant among several does not open every tenant's data. A project-level grant for these routes is #593.

- `require_fga(...)` — fine-grained (ReBAC) authorization via Descope FGA (`dependencies/fga.py`); an FGA error is answered with 502 and treated as a denial.
- `require_admin_role` — a plain dependency in `routers/tenants.py` that checks the token's project-level `roles` claim for `owner` or `admin`; only `POST /api/tenants` uses it.

Every tenant-scoped route must declare one of the first three, and a unit test enforces it ([conventions §15](conventions.md#15-every-tenant-scoped-route-declares-its-authorization)).

## Transport & request hardening

- **Security headers** — `SecurityHeadersMiddleware` sets response hardening headers; `ENVIRONMENT=production` adds HSTS and the strict default CSP (`CSP_POLICY` overrides the CSP).
- **Rate limiting** — [slowapi](https://github.com/laurentS/slowapi): `RATE_LIMIT_DEFAULT` (60/min) and `RATE_LIMIT_AUTH` (10/min for auth-sensitive routes); keyed by the authenticated `sub` or by client IP. `GET /api/health` is exempt. Standalone mode only: in `gateway` mode Tyk does the rate limiting.
- **Trusted proxies** — `TRUSTED_PROXY_HOSTS` controls which forwarded headers are honored.
- **Internal endpoints** — `/api/internal/*` are each gated by a shared secret; an unset secret makes the endpoint reject all traffic (fail-closed).

## Supply-chain / CI scanning

Security scanning runs in CI (`.github/workflows/`), using GitHub-native and FOSS tools:

- `codeql.yml` — CodeQL static analysis
- `osv-scanner.yml` — OSV dependency scanning
- `dependency-review.yml` — PR dependency review
- `scorecard.yml` — OpenSSF Scorecard
- `security.yml` — `pip-audit`, `npm audit`, gitleaks secret scanning and Semgrep (uploaded to code scanning)

Locally, `make security` runs `pip-audit` + `npm audit`.

## Notable dependency pins (decision record)

- **`fastapi>=0.115.0,<0.137`** — capped because slowapi 0.1.10's `SlowAPIMiddleware` route matcher only recognizes top-level routes under the newer FastAPI inclusion model, which breaks rate limiting. Revisit when slowapi ships a fix (`backend/pyproject.toml`).
- **`py-identity-model` 5.0.0 pre-release** — pinned to one commit of the library's repository (`git+https://github.com/jamescrowley321/identity-model@<sha>#subdirectory=py`) in `backend/pyproject.toml` and `backend/uv.lock`, because v5 pre-releases are not published to PyPI and each pre-release's assets are deleted when the next one is cut. The pinned commit carries `TokenValidationConfig.token_issuers` (identity-model#1184), which lets the library check a Descope token's `iss` against all three issuer forms; it moves to a `release/v5` commit once that change is merged there, and is replaced by `>=5.0.0,<6` when 5.0.0 is released (see Token validation above). The installed package reports version 4.1.1: the library sets its version at release time, so the commit, not the version, says what is installed. Installing it needs `git` (the backend image installs it for the build step and removes it). While the pin is a commit, dependency scanning does not cover this one package: `pip-audit` skips a package that is not on PyPI, and Dependabot proposes no update for a git source. Until the pin is a PyPI range, advisories and new commits for the library are followed on [its repository](https://github.com/jamescrowley321/identity-model/security/advisories), and a newer commit is taken by changing the sha in `backend/pyproject.toml`, then running `uv lock` and `uv sync --locked --reinstall-package py-identity-model`. Read `docs/upgrade-v5.md` at the new commit before taking it.
