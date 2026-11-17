import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { useAuth } from "react-oidc-context";
import { useApiClient } from "../hooks/useApiClient";
import { tenantScope } from "../config/oidc";
import { TenantSelectorContext } from "./TenantSelector";

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
}

export interface CanonicalLinkedIdp {
  provider_name: string;
  external_sub: string;
}

/**
 * The tenant the caller's token names and the permissions it carries there.
 *
 * Sent only for a provider whose tenant comes from the token: its presence
 * says that the API acts in this tenant whatever the SPA selects, so switching
 * tenant takes a new token.
 */
export interface CanonicalTokenTenant {
  tenant_id: string;
  permissions: string[];
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
  token_tenant?: CanonicalTokenTenant | null;
}

/**
 * How the active tenant changes.
 *
 * - `client`: the tenant does not come from the token; the selection is
 *   client-side.
 * - `sign-in`: the tenant comes from the token and a tenant scope template is
 *   configured; switching signs in again with that tenant's scope.
 * - `none`: the tenant comes from the token and no template is configured;
 *   the current tenant is shown and no switch is offered.
 */
export type TenantSwitchMode = "client" | "sign-in" | "none";

/** A sign-in to one tenant whose token does not name that tenant. */
export interface TenantSwitchFailure {
  requestedTenantId: string;
}

/** What a sign-in to one tenant carries through the redirect (`User.state`). */
interface TenantSignInState {
  requestedTenantId: string;
}

/** A `GET /api/identity` answer other than 2xx; `status` is its HTTP status. */
class IdentityFetchError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`identity fetch failed: ${status}`);
    this.name = "IdentityFetchError";
    this.status = status;
  }
}

/** The API refused the caller (401/403), as opposed to failing to answer. */
function isRefusal(error: Error | null): boolean {
  return error instanceof IdentityFetchError && (error.status === 401 || error.status === 403);
}

/** The verdict on one tenant sign-in, fixed by the first identity result that settles it. */
interface SwitchVerdict {
  subject: string | null;
  requestedTenantId: string;
  failed: boolean;
}

function requestedTenantOf(state: unknown): string | null {
  if (typeof state !== "object" || state === null) return null;
  const requested = (state as Partial<TenantSignInState>).requestedTenantId;
  return typeof requested === "string" ? requested : null;
}

interface IdentityContextValue {
  identity: CanonicalIdentity | null;
  loading: boolean;
  error: Error | null;
  /**
   * Active tenant: the token's tenant when it comes from the token, otherwise
   * the client-side selection, defaulting to the first membership.
   */
  currentTenantId: string | null;
  /** Switch the active tenant (persisted to localStorage, validated on read). */
  setCurrentTenantId: (tenantId: string) => void;
  /** How the active tenant changes for this caller. */
  tenantSwitch: TenantSwitchMode;
  /** Sign in again for one tenant; does nothing unless `tenantSwitch` is `sign-in`. */
  signInToTenant: (tenantId: string) => Promise<void>;
  /** Sign in again with the configured scope, asking for no tenant. */
  signInAgain: () => Promise<void>;
  /** The last tenant sign-in, when its token does not name the tenant asked for. */
  tenantSwitchFailure: TenantSwitchFailure | null;
}

const IdentityContext = createContext<IdentityContextValue | null>(null);

/** localStorage key for the persisted active-tenant selection. */
const STORAGE_KEY = "identity.currentTenantId";

function readStoredTenantId(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

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
export function IdentityProvider({
  children,
  tenantScopeTemplate = null,
}: {
  children: ReactNode;
  /** `VITE_OIDC_TENANT_SCOPE_TEMPLATE`, read by `readTenantScopeTemplate`. */
  tenantScopeTemplate?: string | null;
}) {
  const auth = useAuth();
  const { apiFetch } = useApiClient();

  const isAuthenticated = auth.isAuthenticated;
  // The authenticated subject. Keying the fetch on it forces a reload — and a
  // reset of the cached identity — whenever a *different* user logs in on the
  // same tab (SPA local-logout → login, no page reload), so one user's roles/
  // tenants/email can never be surfaced to the next.
  const subject = auth.user?.profile?.sub ?? null;
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
  // The tenant the current sign-in asked for (none for an ordinary sign-in),
  // read by the fetch below without making it a reason to refetch.
  const requestedTenantId = requestedTenantOf(auth.user?.state);
  const requestedTenantRef = useRef(requestedTenantId);
  useEffect(() => {
    requestedTenantRef.current = requestedTenantId;
  }, [requestedTenantId]);
  const [verdict, setVerdict] = useState<SwitchVerdict | null>(null);

  // Identity is reloaded when the subject changes, not on token renewal. That
  // holds for a token-borne tenant only while renewal keeps the token's tenant,
  // as a refresh-token renewal does (Keycloak keeps the organization). A silent
  // sign-in renewal asks with the base scope and could change it: #602.
  useEffect(() => {
    // Only fetch when authenticated — avoids an apiFetch /login bounce.
    if (!isAuthenticated) return;

    let cancelled = false;

    // A tenant sign-in is judged on the first identity result that settles it:
    // identity loaded (its token names the tenant asked for, or another or
    // none) or identity refused (401/403). Any other failure (5xx, network)
    // settles nothing. Once settled the verdict stands: User.state keeps the
    // tenant asked for for the whole session, so a later load is not judged
    // as the sign-in again.
    const settle = (failed: boolean) => {
      const requested = requestedTenantRef.current;
      if (requested === null) return;
      setVerdict((prev) =>
        prev !== null && prev.subject === subject && prev.requestedTenantId === requested
          ? prev
          : { subject, requestedTenantId: requested, failed },
      );
    };

    const loadIdentity = async () => {
      // Drop any prior subject's identity/error before the new fetch resolves,
      // so a stale principal is never exposed during the load window.
      setFetchedIdentity(null);
      setFetchError(null);
      setFetchLoading(true);
      try {
        const res = await apiFetch("/api/identity");
        if (!res.ok) throw new IdentityFetchError(res.status);
        const data = (await res.json()) as CanonicalIdentity;
        if (!cancelled) {
          setFetchedIdentity(data);
          setFetchedSubject(subject);
          setFetchError(null);
          settle(data.token_tenant?.tenant_id !== requestedTenantRef.current);
        }
      } catch (err: unknown) {
        if (cancelled) return;
        // Fail-closed — drop identity so no roles/tenants are surfaced.
        setFetchedIdentity(null);
        setFetchedSubject(subject);
        const failure = err instanceof Error ? err : new Error(String(err));
        setFetchError(failure);
        if (isRefusal(failure)) settle(true);
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

  const tokenTenantId = identity?.token_tenant?.tenant_id ?? null;

  const currentTenantId = useMemo(() => {
    // The API acts in the token's tenant: no client-side selection overrides it.
    if (tokenTenantId !== null) return tokenTenantId;
    const memberships = identity?.tenant_memberships ?? [];
    if (memberships.length === 0) return null;
    const memberIds = new Set(memberships.map((m) => m.tenant_id));
    // Honor the client-side selection only while it remains a valid membership;
    // a stale selection falls back to the first membership.
    if (selectedTenantId && memberIds.has(selectedTenantId)) return selectedTenantId;
    return memberships[0].tenant_id;
  }, [identity, tokenTenantId, selectedTenantId]);

  const setCurrentTenantId = useCallback((tenantId: string) => {
    setSelectedTenantId(tenantId);
    try {
      window.localStorage.setItem(STORAGE_KEY, tenantId);
    } catch {
      // localStorage unavailable — selection persists for this session only.
    }
  }, []);

  const tenantSwitch: TenantSwitchMode =
    tokenTenantId === null ? "client" : tenantScopeTemplate === null ? "none" : "sign-in";

  const signinRedirect = auth.signinRedirect;

  const signInToTenant = useCallback(
    async (tenantId: string) => {
      if (tenantSwitch !== "sign-in" || tenantScopeTemplate === null) return;
      const state: TenantSignInState = { requestedTenantId: tenantId };
      await signinRedirect({ scope: tenantScope(tenantScopeTemplate, tenantId), state });
    },
    [tenantSwitch, tenantScopeTemplate, signinRedirect],
  );

  const signInAgain = useCallback(() => signinRedirect(), [signinRedirect]);

  // The provider may answer a sign-in for a tenant the user does not belong
  // to with a token naming another tenant or none, instead of an error.
  const failedSwitch =
    verdict !== null &&
    verdict.failed &&
    verdict.subject === subject &&
    verdict.requestedTenantId === requestedTenantId &&
    !loading;
  const tenantSwitchFailure = useMemo<TenantSwitchFailure | null>(
    () => (failedSwitch && requestedTenantId !== null ? { requestedTenantId } : null),
    [failedSwitch, requestedTenantId],
  );

  const value = useMemo<IdentityContextValue>(
    () => ({
      identity,
      loading,
      error,
      currentTenantId,
      setCurrentTenantId,
      tenantSwitch,
      signInToTenant,
      signInAgain,
      tenantSwitchFailure,
    }),
    [
      identity,
      loading,
      error,
      currentTenantId,
      setCurrentTenantId,
      tenantSwitch,
      signInToTenant,
      signInAgain,
      tenantSwitchFailure,
    ],
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
