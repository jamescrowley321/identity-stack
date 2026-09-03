/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Generic OIDC provider configuration (FR-15). When set, these target the
   * login flow at any standard-OIDC provider and take precedence over the
   * `VITE_DESCOPE_*` variables below, which remain as a backward-compat
   * fallback (NFR-8). No provider is hardcoded (NFR-5).
   */
  readonly VITE_OIDC_AUTHORITY?: string;
  readonly VITE_OIDC_CLIENT_ID?: string;
  readonly VITE_OIDC_SCOPE?: string;
  readonly VITE_OIDC_REDIRECT_URI?: string;

  readonly VITE_DESCOPE_PROJECT_ID?: string;
  readonly VITE_DESCOPE_BASE_URL?: string;
  /**
   * Absolute base URL the browser uses for API calls.
   *
   * - Empty string (the default in standalone mode): the frontend uses
   *   relative `/api/...` URLs, which nginx's `/api/` proxy forwards to
   *   the backend on the same origin.
   * - `http://localhost:8080` (set by docker-compose.gateway.yml in
   *   gateway mode): the browser hits Tyk directly.
   *
   * Vite inlines this value at build time, so switching modes requires
   * rebuilding the frontend container — `make dev-gateway` and CI's
   * `make test-integration-gateway` both pass `--build`.
   */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
