## Review: Security (Sentinel)

### BLOCK (must fix before merge)

- [CONFIRMED] `backend/app/routers/internal.py:75` + `backend/app/middleware/factory.py:74-76` — Flow sync endpoint (`POST /api/internal/users/sync`) has zero authentication: no JWT, no HMAC, no API key, no shared secret.
  Attack scenario: An attacker who can reach the backend (any service on the Docker network, any localhost process, or the backend if port 8000 is exposed in production) sends `POST /api/internal/users/sync` with `{"user_id": "attacker-sub", "email": "victim@corp.com"}`. This creates a canonical user or hijacks an existing one by linking an attacker-controlled Descope identity to the victim's email. On subsequent legitimate syncs for that email, the existing user is found by email and a second IdP link is created, or the attacker's link is already present. The attacker can then trigger updates to the victim's profile via the same unauthenticated endpoint.
  Impact: User account takeover / identity impersonation. An attacker can inject arbitrary user records and link them to any external identity, or modify existing users' emails and names. The comment says "network-level isolation" but the docker-compose.yml shows the backend on a shared Docker network with no ingress restrictions. In production, without a reverse proxy rule explicitly blocking `/api/internal/`, this endpoint is internet-facing.

### WARN (should fix)

- [LIKELY] `backend/app/routers/internal.py:75` — No rate limiting on flow sync endpoint.
  Every other write endpoint in the codebase uses `@limiter.limit(RATE_LIMIT_AUTH)`. The internal endpoints have no rate limit at all, and since they also bypass JWT auth, the rate limiter's `get_rate_limit_key` will fall back to IP address. Without rate limiting, an attacker (or a misconfigured Descope flow) can flood the endpoint and create unbounded user rows.
  Mitigation: Add `@limiter.limit(RATE_LIMIT_AUTH)` (or a custom internal limit) to both internal endpoints.

- [LIKELY] `backend/app/services/inbound_sync.py:176,191,239` — Webhook data dict logged at WARNING level when fields are missing (`logger.warning("user.created webhook missing email or user_id: %s", data)`). The `data` dict is an untyped `dict` from the webhook payload and could contain PII (email, name, phone) or unexpected fields. Logging the entire dict to application logs risks PII exposure in log aggregators.
  Mitigation: Log only the keys present in `data` or specific non-sensitive identifiers, not the full dict. E.g., `logger.warning("user.created webhook missing email or user_id, keys=%s", list(data.keys()))`.

- [UNLIKELY] `backend/app/services/inbound_sync.py:59-60` — OTel span attributes include `user.email` in plaintext (`span.set_attribute("user.email", email)`). If OTel traces are exported to a shared dashboard (the docker-compose Aspire dashboard uses `Unsecured` auth mode), email addresses are visible to anyone who can access the dashboard.
  Mitigation: Hash or omit the email from span attributes. Use `span.set_attribute("user.email_domain", email.split("@")[1])` if domain-level tracing suffices.

### INFO (acceptable risk)

- `backend/app/services/inbound_sync.py:207-209` — The `_handle_user_updated` path reads `email` from the untyped webhook `data` dict and writes it directly to the user model without email format validation (unlike the flow sync path which uses Pydantic `EmailStr`). However, the `User.email` column has a unique constraint and is a plain `sa.String`, so a malformed email would be stored but would not enable injection (SQLAlchemy ORM parameterizes all values). The risk is data quality degradation, not a security exploit.

- `backend/app/middleware/auth.py:43` — The `excluded_prefixes` implementation uses `str.startswith(tuple)`, which is correct and not vulnerable to path traversal. Paths like `/api/internal/../protected` are normalized by ASGI servers before reaching the middleware, so prefix bypass via path traversal is not exploitable.

- `backend/app/routers/internal.py:60-63` — When `DESCOPE_WEBHOOK_SECRET` is empty/unset, the webhook endpoint rejects all requests with 401. This is correct fail-closed behavior.

- `backend/app/routers/internal.py:66-68` — HMAC validation uses `hmac.compare_digest` for timing-safe comparison. This is correct and prevents timing side-channel attacks on the signature.

### Summary
- BLOCK: 1 | WARN: 3 | INFO: 4
- Overall: FAIL
