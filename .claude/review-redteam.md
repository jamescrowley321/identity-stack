## Review: Red Team (Viper)

### Attack Surface

1. **`/api/internal/users/sync` (POST)** — New unauthenticated endpoint. JWT auth bypassed via `excluded_prefixes` in `TokenValidationMiddleware`. No alternative authentication mechanism (no HMAC, no API key, no mTLS). Creates/updates canonical users and creates IdP links. Network-reachable from frontend Nginx proxy (`location /api/` proxies all `/api/*` paths to backend).

2. **`/api/internal/webhooks/descope` (POST)** — New endpoint, JWT bypassed, protected only by HMAC-SHA256 signature validation. Processes user lifecycle events (create, update, delete/deactivate).

3. **`TokenValidationMiddleware.excluded_prefixes`** — New prefix-based auth bypass mechanism added to the core auth middleware. Changes the security boundary from an explicit allowlist of paths to a prefix wildcard that covers all current and future routes under `/api/internal/`.

4. **`InboundSyncService.sync_user_from_flow()`** — Service-layer user creation/update logic reachable without authentication. Accepts arbitrary `user_id` and `email` to create users and IdP links.

5. **Nginx reverse proxy** — Frontend Nginx config (`location /api/`) forwards all `/api/*` requests to backend, including `/api/internal/*`. No network-level isolation exists for internal endpoints.

### Findings

#### CRITICAL — Unauthenticated User and IdP Link Injection via Flow Sync Endpoint

- **Location**: `backend/app/routers/internal.py:75-95`, `backend/app/middleware/factory.py:74-76`
- **Attack scenario**:
  1. Attacker identifies the `/api/internal/users/sync` endpoint (discoverable via `/openapi.json` which lists all routes, or by reading the `Internal` tag in the OpenAPI schema).
  2. Attacker sends `POST /api/internal/users/sync` with `{"user_id": "U2target123", "email": "attacker@evil.com"}`. No Authorization header needed. The `excluded_prefixes` bypass in `TokenValidationMiddleware` (line 43 of `auth.py`) skips JWT validation entirely.
  3. The service creates a canonical user with `email=attacker@evil.com` and an IdP link mapping Descope external subject `U2target123` to that user. When the legitimate Descope user `U2target123` later triggers a flow sync or webhook, the IdP link already exists. The existing-link path (line 84-104 of `inbound_sync.py`) updates the attacker-created user record with the legitimate user's email, effectively letting the attacker pre-register and control the canonical identity for any Descope subject.
  4. Alternatively, the attacker sends `{"user_id": "attacker-descope-id", "email": "victim@company.com"}`. If a canonical user with `victim@company.com` exists, the email lookup (line 107) finds them, and an IdP link is created binding the attacker's Descope identity to the victim's canonical user. The attacker now has a linked identity to the victim's account.
- **Prerequisites**: Network access to the backend (reachable via `frontend:8080/api/internal/users/sync` through Nginx proxy, or directly to `backend:8000` on the Docker network). No credentials needed.
- **CVSS v3.1**: 9.8 (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H)
- **Remediation**: Add authentication to the flow sync endpoint. Options in order of preference: (a) Require a shared secret header (e.g., `X-Flow-Secret`) validated against an env var, similar to the webhook HMAC pattern. (b) Use IP allowlisting in middleware for the flow sync path. (c) Add mTLS between Descope Flow HTTP Connector and the backend. The current design relies on "network-level isolation" per the E2E test comments, but no such isolation exists in the Docker Compose or Nginx configuration.

#### HIGH — Frontend Nginx Proxy Exposes Internal Endpoints to the Internet

- **Location**: `frontend/nginx.conf:6-9`, `backend/app/middleware/factory.py:74-76`
- **Attack scenario**:
  1. The Nginx config forwards all requests matching `location /api/` to the backend. This includes `/api/internal/*`.
  2. An external attacker sends `POST http://<frontend-host>:3000/api/internal/users/sync` with a crafted JSON body.
  3. Nginx proxies the request to `backend:8000/api/internal/users/sync`. The backend's `TokenValidationMiddleware` skips JWT validation because the path starts with `/api/internal/`. The request hits the flow sync endpoint with no authentication.
  4. This eliminates the "network isolation" defense that the code comments and E2E tests claim exists.
- **Prerequisites**: Network access to the frontend's public port (3000 in Docker Compose, or 8080 on Nginx inside the container).
- **CVSS v3.1**: 8.1 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:H)
- **Remediation**: Add a `location /api/internal/` block to `nginx.conf` that returns 403, preventing the frontend proxy from forwarding internal requests. Example:
  ```nginx
  location /api/internal/ {
      return 403;
  }
  ```
  Place this block before the generic `location /api/` block in the Nginx config. This provides defense-in-depth even after application-level auth is added.

#### HIGH — IdP Link Hijacking via Unauthenticated Email-Based User Lookup

- **Location**: `backend/app/services/inbound_sync.py:107-137`
- **Attack scenario**:
  1. Attacker knows (or guesses) a victim's email address that exists in the canonical user table.
  2. Attacker sends `POST /api/internal/users/sync` with `{"user_id": "attacker-controlled-descope-id", "email": "victim@company.com"}`.
  3. The service finds no existing IdP link for `attacker-controlled-descope-id` (line 82), then looks up the user by email (line 107), finds the victim's user record, and creates an IdP link binding the attacker's Descope identity to the victim's canonical user (lines 127-136).
  4. If the application later uses IdP link lookups for authorization decisions or session mapping, the attacker's Descope identity is now associated with the victim's canonical user, enabling account takeover.
- **Prerequisites**: Network access to the endpoint (no auth needed), knowledge of victim's email. The endpoint is unauthenticated, so this can be automated at scale.
- **CVSS v3.1**: 8.1 (AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N)
- **Remediation**: This is a direct consequence of Finding 1 (no auth on the flow sync endpoint). Adding authentication resolves this. Additionally, consider requiring that the `user_id` in the flow sync payload be cryptographically verified (e.g., signed by Descope) rather than accepted as a plain string.

#### MEDIUM — Webhook HMAC Secret Not Validated at Startup

- **Location**: `backend/app/routers/internal.py:60-63`
- **Attack scenario**:
  1. Operator deploys the application without setting `DESCOPE_WEBHOOK_SECRET` (easy to miss since it is a new env var not previously required).
  2. The application starts without error. No log warning at startup.
  3. All webhook requests are rejected with 401 ("Webhook secret not configured"). Descope webhook events (user.created, user.updated, user.deleted) are silently dropped.
  4. The canonical user store drifts out of sync with Descope. Users deleted in Descope remain active in the canonical store. Users updated in Descope retain stale data.
  5. While not directly exploitable for unauthorized access, this creates a silent failure mode that could be leveraged: an attacker who compromises a Descope account could rely on the fact that the `user.deleted` webhook never deactivates their canonical record, maintaining persistent access even after Descope-side remediation.
- **Prerequisites**: Operator fails to set the env var (likely during initial deployment or environment migration).
- **CVSS v3.1**: 5.3 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:L)
- **Remediation**: Validate `DESCOPE_WEBHOOK_SECRET` is set and non-empty at application startup (in the lifespan handler or factory). Log a warning or raise an error if missing. Do not rely on per-request env reads.

#### MEDIUM — No Rate Limiting on Internal Endpoints

- **Location**: `backend/app/routers/internal.py:75-113`
- **Attack scenario**:
  1. The internal router does not use `@limiter.limit()` or `@limiter.exempt` decorators (confirmed by searching the file). Every other mutating endpoint in the codebase uses `@limiter.limit(RATE_LIMIT_AUTH)`.
  2. The rate limiter key function (`get_rate_limit_key` in `rate_limit.py`) falls back to IP address when no JWT claims are present. Since internal endpoints bypass JWT, `request.state.claims` is never populated, so rate limiting keys on IP.
  3. However, the SlowAPI default rate limit (60/minute) applies globally. An attacker can still send 60 requests per minute per IP address. With multiple IPs or behind a shared proxy (where `X-Real-IP` may be spoofable), this could be amplified.
  4. Attacker automates calls to `/api/internal/users/sync` to create a large number of users and IdP links, polluting the canonical store with garbage data or exhausting database resources.
- **Prerequisites**: Network access to the endpoint. No credentials needed (see Finding 1).
- **CVSS v3.1**: 5.3 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:L)
- **Remediation**: Add explicit rate limiting to internal endpoints. Use `@limiter.limit("5/minute")` or similar restrictive limit on both flow sync and webhook endpoints, since these are machine-to-machine integrations with predictable traffic patterns. This provides defense-in-depth alongside authentication.

#### LOW — PII in OTel Spans Enables Secondary Data Exfiltration

- **Location**: `backend/app/services/inbound_sync.py:59`
- **Attack scenario**:
  1. The flow sync service sets `span.set_attribute("user.email", email)` on every invocation, writing raw email addresses to distributed traces.
  2. The OTel exporter sends traces to the Aspire Dashboard (`http://aspire-dashboard:18889`), which is configured with `AUTHMODE=Unsecured`.
  3. An attacker with access to the Docker network (or any service that can reach port 18888/18889) can read all traces including email addresses of every user synced through the flow endpoint.
  4. Combined with Finding 1 (unauthenticated endpoint), an attacker who injects users via the flow sync endpoint would also see their injected data confirmed in traces, and could see legitimate user emails from real sync operations.
- **Prerequisites**: Access to the OTel dashboard (Docker network access in dev; production exposure depends on deployment).
- **CVSS v3.1**: 3.7 (AV:A/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N)
- **Remediation**: Hash or redact email in span attributes. Use `span.set_attribute("user.email_hash", hashlib.sha256(email.encode()).hexdigest()[:12])` for correlation without PII exposure. Also applies to `span.set_attribute("descope.user_id", user_id)` if the Descope user ID is considered sensitive.

### Summary
- Attack surface elements: 5
- Findings: 1 critical, 2 high, 2 medium, 1 low
- Overall: **FAIL**

The critical finding is the completely unauthenticated `/api/internal/users/sync` endpoint that allows arbitrary user creation and IdP link injection. This is compounded by the Nginx proxy forwarding internal paths to the public internet, eliminating the network isolation defense. The webhook endpoint's HMAC validation is correctly implemented but the flow sync endpoint has no equivalent protection. These issues must be resolved before this change is safe to deploy.
