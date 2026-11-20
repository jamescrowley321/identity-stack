import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";
import { useAuth } from "react-oidc-context";
import { useApiClient } from "../hooks/useApiClient";
import { TenantSelectorContext } from "./TenantSelector";
import { initialTenantId, readStoredTenantId, signInOrganization, storeTenantId } from "./tenantSelection";

/**
 * Shape of the canonical `GET /api/identity` payload (provider-neutral).
 *
 * The backend resolves the authenticated principal — Ory or Descope — to
 * this identical structure, so the frontend never decodes provider-specific
 * token claims (`dct`/`tenants`) directly. A tenant may appear in multiple
 * `roles` entries (one per role assignment).
 */
export interface CanonicalRole {
  tenant_id: string;
  role_name: string;
  permissions: string[];
}

export interface CanonicalTenantMembership {
  tenant_id: string;
  tenant_name: string;
  /** The tenant's organization at the caller's provider; `null` (or absent) when it has none there. */
  external_org_id?: string | null;
}

export interface CanonicalLinkedIdp {
  provider_name: string;
  external_sub: string;
}

export interface CanonicalIdentity {
  user: {
    id: string;
    email: string;
    user_name: string | null;
    given_name: string | null;
    family_name: string | null;
    status: string;
  };
  roles: CanonicalRole[];
  tenant_memberships: CanonicalTenantMembership[];
  linked_idps: CanonicalLinkedIdp[];
}

interface IdentityContextValue {
  identity: CanonicalIdentity | null;
  loading: boolean;
  error: Error | null;
  /**
   * Active tenant — client-side selection; defaults to the membership whose
   * organization the sign-in named, else the first membership.
   */
  currentTenantId: string | null;
  /** Switch the active tenant (persisted to localStorage, validated on read). */
  setCurrentTenantId: (tenantId: string) => void;
}

const IdentityContext = createContext<IdentityContextValue | null>(null);

/**
 * Fetches the canonical identity once per authenticated session and exposes
 * it, plus the client-side active-tenant selection, to descendant hooks
 * (`useTenants`, `useRBAC`) and components (`TenantSwitcher`).
 *
 * Must be mounted inside `<BrowserRouter>` because the authenticated fetch
 * (`useApiClient`) depends on `useNavigate`.
 *
 * Fail-closed: any fetch error, 401, or unauthenticated state leaves
 * `identity` null, so consumers surface no roles or tenants.
 */
export function IdentityProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const { apiFetch } = useApiClient();

  const isAuthenticated = auth.isAuthenticated;
  // The authenticated subject. Keying the fetch on it forces a reload — and a
  // reset of the cached identity — whenever a *different* user logs in on the
  // same tab (SPA local-logout → login, no page reload), so one user's roles/
  // tenants/email can never be surfaced to the next.
  const subject = auth.user?.profile?.sub ?? null;
  // The organization the sign-in named (Keycloak `organization`, Zitadel
  // `urn:zitadel:iam:user:resourceowner:id`): only where the selector starts.
  const organization = signInOrganization(auth.user?.profile, auth.user?.access_token);
  const [fetchedIdentity, setFetchedIdentity] = useState<CanonicalIdentity | null>(null);
  // The subject the cached identity/error belongs to. Recorded at fetch
  // completion so the render-time gate below can reject a cache left over from
  // a different user — closing the one-frame window between an in-tab subject
  // switch and the effect that reloads.
  const [fetchedSubject, setFetchedSubject] = useState<string | null>(null);
  const [fetchLoading, setFetchLoading] = useState(false);
  const [fetchError, setFetchError] = useState<Error | null>(null);
  const [selectedTenantId, setSelectedTenantId] = useState<string | null>(() =>
    readStoredTenantId(),
  );

  // Identity is reloaded when the subject changes, not on token renewal: the
  // token says only who the caller is, and the active tenant is the client's
  // selection, so a renewed token changes neither.
  useEffect(() => {
    // Only fetch when authenticated — avoids an apiFetch /login bounce.
    if (!isAuthenticated) return;

    let cancelled = false;

    const loadIdentity = async () => {
      // Drop any prior subject's identity/error before the new fetch resolves,
      // so a stale principal is never exposed during the load window.
      setFetchedIdentity(null);
      setFetchError(null);
      setFetchLoading(true);
      // A new sign-in has dropped the persisted selection (onSigninCallback).
      setSelectedTenantId(readStoredTenantId());
      try {
        const res = await apiFetch("/api/identity");
        if (!res.ok) throw new Error(`identity fetch failed: ${res.status}`);
        const data = (await res.json()) as CanonicalIdentity;
        if (!cancelled) {
          setFetchedIdentity(data);
          setFetchedSubject(subject);
          setFetchError(null);
        }
      } catch (err: unknown) {
        if (cancelled) return;
        // Fail-closed — drop identity so no roles/tenants are surfaced.
        setFetchedIdentity(null);
        setFetchedSubject(subject);
        setFetchError(err instanceof Error ? err : new Error(String(err)));
      } finally {
        if (!cancelled) setFetchLoading(false);
      }
    };

    void loadIdentity();

    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, subject, apiFetch]);

  // Fail-closed: surface the cached identity/error ONLY while authenticated AND
  // the cache belongs to the current subject. Gating on the subject at render
  // time (not just in the effect) means a payload fetched for a previous user
  // is never painted for the next — even for the single frame between an in-tab
  // subject switch and the reload effect.
  const cacheMatchesSubject = fetchedSubject === subject;
  const identity = isAuthenticated && cacheMatchesSubject ? fetchedIdentity : null;
  const error = isAuthenticated && cacheMatchesSubject ? fetchError : null;
  // Loading while authenticated until a result (identity or error) has landed
  // for the CURRENT subject — this also covers the transient post-switch frame,
  // so consumers never read identity=null + loading=false as a genuine
  // no-access state (flash of stripped roles).
  const resolvedForSubject =
    cacheMatchesSubject && (fetchedIdentity !== null || fetchError !== null);
  const loading = isAuthenticated ? fetchLoading || !resolvedForSubject : false;

  // Honor the client-side selection only while it remains a valid membership;
  // otherwise start on the sign-in's organization, else the first membership.
  const currentTenantId = useMemo(
    () => initialTenantId(identity?.tenant_memberships ?? [], selectedTenantId, organization),
    [identity, selectedTenantId, organization],
  );

  const setCurrentTenantId = useCallback((tenantId: string) => {
    setSelectedTenantId(tenantId);
    storeTenantId(tenantId);
  }, []);

  const value = useMemo<IdentityContextValue>(
    () => ({ identity, loading, error, currentTenantId, setCurrentTenantId }),
    [identity, loading, error, currentTenantId, setCurrentTenantId],
  );

  // Every API call below names the active tenant; the identity fetch above,
  // outside this provider, names none.
  return (
    <IdentityContext.Provider value={value}>
      <TenantSelectorContext.Provider value={currentTenantId}>{children}</TenantSelectorContext.Provider>
    </IdentityContext.Provider>
  );
}

/**
 * Access the canonical identity context. Throws if used outside an
 * `<IdentityProvider>` so mis-wiring surfaces immediately.
 */
export function useIdentityContext(): IdentityContextValue {
  const ctx = useContext(IdentityContext);
  if (ctx === null) {
    throw new Error("useIdentityContext must be used within an IdentityProvider");
  }
  return ctx;
}
