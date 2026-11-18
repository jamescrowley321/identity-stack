/**
 * Provider-driven OIDC client configuration (FR-15).
 *
 * The login flow targets whichever standard-OIDC provider is configured via the
 * generic `VITE_OIDC_*` variables. When those are unset, we fall back to the
 * existing `VITE_DESCOPE_*` variables so any deployment configured before this
 * change keeps working byte-for-byte (NFR-8). No provider name is hardcoded as a
 * target (NFR-5) — the Descope keys are the documented backward-compat fallback,
 * not a preferred IdP.
 */

const DESCOPE_DEFAULT_BASE_URL = "https://api.descope.com";
const DEFAULT_SCOPE = "openid profile email";

/** Subset of `import.meta.env` this builder reads. */
export interface OidcEnv {
  readonly VITE_OIDC_AUTHORITY?: string;
  readonly VITE_OIDC_CLIENT_ID?: string;
  readonly VITE_OIDC_SCOPE?: string;
  readonly VITE_OIDC_REDIRECT_URI?: string;
  readonly VITE_DESCOPE_PROJECT_ID?: string;
  readonly VITE_DESCOPE_BASE_URL?: string;
}

export interface OidcConfig {
  authority: string;
  client_id: string;
  redirect_uri: string;
  scope: string;
  response_type: string;
  automaticSilentRenew: boolean;
  onSigninCallback: () => void;
}

/**
 * Build the `react-oidc-context` `AuthProvider` config from environment.
 *
 * @param env    Vite environment (`import.meta.env`).
 * @param origin Redirect origin, defaults to the current window origin.
 */
export function buildOidcConfig(
  env: OidcEnv,
  origin: string = window.location.origin,
): OidcConfig {
  const descopeAuthority = `${env.VITE_DESCOPE_BASE_URL || DESCOPE_DEFAULT_BASE_URL}/${env.VITE_DESCOPE_PROJECT_ID}`;

  // `||` (not `??`) so a bare `VITE_OIDC_*=` line — which Vite materializes as
  // an empty string — is treated as unset and falls back to the Descope/default
  // value, preserving the NFR-8 backward-compat contract. `??` would keep the
  // empty string and silently break login (empty authority / non-OIDC scope).
  return {
    authority: env.VITE_OIDC_AUTHORITY || descopeAuthority,
    client_id: env.VITE_OIDC_CLIENT_ID || env.VITE_DESCOPE_PROJECT_ID || "",
    redirect_uri: env.VITE_OIDC_REDIRECT_URI || origin,
    scope: env.VITE_OIDC_SCOPE || DEFAULT_SCOPE,
    response_type: "code",
    automaticSilentRenew: true,
    onSigninCallback: () => {
      // Remove code/state from URL after successful code exchange
      window.history.replaceState({}, document.title, window.location.pathname);
    },
  };
}
