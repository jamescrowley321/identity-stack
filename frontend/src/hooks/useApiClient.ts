import { useAuth } from "react-oidc-context";
import { useCallback, useRef, useEffect } from "react";
import { useNavigate } from "react-router-dom";

/**
 * Hook that provides an authenticated fetch wrapper.
 *
 * - Attaches the current access token as a Bearer header.
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

      const makeRequest = (t: string) =>
        fetch(path, {
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
