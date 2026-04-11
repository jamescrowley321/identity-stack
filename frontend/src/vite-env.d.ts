/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DESCOPE_PROJECT_ID: string;
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
