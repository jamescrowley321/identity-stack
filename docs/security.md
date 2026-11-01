# Security

This documents the application-level security controls. For vulnerability reporting see [`../SECURITY.md`](../SECURITY.md).

## Token validation

Bearer tokens are validated by [`py-identity-model`](https://github.com/jamescrowley321/py-identity-model) (`>=3.8.5`, a security-pinned floor — it pulls `cryptography>=50` with the PYSEC-2026-3552/3553/3554 fixes). Validation accepts all three Descope issuer formats (all signed by the same project JWKS — the signature is the security boundary, not the issuer string):

- `https://api.descope.com/{project_id}` — OIDC / ID tokens
- `https://api.descope.com/v1/apps/{project_id}` — OIDC inbound-app tokens
- `{project_id}` — the **bare project id**, used by SDK / session / access-key tokens (e.g. the `sessionJwt` from the access-key exchange endpoint)

When `ORY_ISSUER_URL` is set, Ory access tokens are accepted too: one issuer, with the audience enforced unless `ORY_REQUIRE_AUDIENCE` is off (`middleware/providers.py`).

In `gateway` mode, Tyk verifies the token signature at the edge. The backend's `GatewayClaimsMiddleware` decodes the forwarded JWT without re-checking the signature, and still checks expiry, issuer and audience as defence in depth (`middleware/factory.py`, `middleware/claims.py`).

## Authorization (RBAC + FGA)

Route handlers enforce authorization with dependency guards built over the validated claims:

- `require_role(...)` — role checks against the token's `tenants`/`dct` claims, or against the canonical model for providers in `CANONICAL_RBAC_PROVIDERS` (`dependencies/rbac.py`).
- `require_permission(...)` — permission checks, resolved the same way.
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
- **`py-identity-model>=3.8.5,<4`** — security floor (see Token validation above).
