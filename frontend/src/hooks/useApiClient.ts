import { useAuth } from "react-oidc-context";
import { useCallback, useRef, useEffect } from "react";
import { useNavigate } from "react-router-dom";

/**
 * Absolute base URL the browser uses for API calls.
 *
 * Empty string (the standalone default) means callers pass `/api/...`
 * paths directly to fetch, which the browser resolves against the
 * current origin — nginx then proxies `/api/` to the backend.
 *
 * In gateway mode, docker-compose.gateway.yml sets VITE_API_BASE_URL
 * to `http://localhost:8080`, so the browser sends requests directly
 * to Tyk. Vite inlines this constant at build time.
 *
 * Trailing slash is stripped so callers can always pass `/api/foo`
 * without producing `//api/foo`.
 */
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

/**
 * Build a full request URL by prepending API_BASE_URL to a relative
 * path. Exported for use by raw fetch call sites that don't go through
 * the apiFetch hook (e.g. unauthenticated /api/health probes).
 */
export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

/**
 * Hook that provides an authenticated fetch wrapper.
 *
 * - Attaches the current access token as a Bearer header.
 * - Prepends API_BASE_URL to the request path so the same call site
 *   works in both standalone (relative URL → nginx proxy) and gateway
 *   (absolute URL → Tyk) modes.
 * - On a 401 response, attempts a silent token renewal and retries once.
 * - If renewal fails, clears the session and navigates to /login.
 */
export function useApiClient() {
  const auth = useAuth();
  const navigate = useNavigate();

  // Keep a ref so the callback always sees the latest auth state
  // without needing auth in its dependency array (avoids re-creating
  // the callback on every token refresh).
  const authRef = useRef(auth);
  useEffect(() => {
    authRef.current = auth;
  }, [auth]);

  const apiFetch = useCallback(
    async (path: string, options: RequestInit = {}): Promise<Response> => {
      const currentAuth = authRef.current;
      const token = currentAuth.user?.access_token;

      if (!token) {
        navigate("/login");
        return Promise.reject(new Error("No access token"));
      }

      const url = apiUrl(path);

      const makeRequest = (t: string) =>
        fetch(url, {
          ...options,
          headers: { ...options.headers, Authorization: `Bearer ${t}` },
        });

      const response = await makeRequest(token);

      if (response.status === 401) {
        try {
          const user = await currentAuth.signinSilent();
          if (user?.access_token) {
            return makeRequest(user.access_token);
          }
        } catch {
          // Silent renewal failed — session is unrecoverable.
        }
        await currentAuth.removeUser();
        navigate("/login", { state: { sessionExpired: true } });
      }

      return response;
    },
    [navigate],
  );

  return { apiFetch };
}
