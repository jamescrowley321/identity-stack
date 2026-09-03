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
  /** Active tenant — client-side selection, defaults to the first membership. */
  currentTenantId: string | null;
  /** Switch the active tenant (persisted to localStorage, validated on read). */
  setCurrentTenantId: (tenantId: string) => void;
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
export function IdentityProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const { apiFetch } = useApiClient();

  const isAuthenticated = auth.isAuthenticated;
  const [fetchedIdentity, setFetchedIdentity] = useState<CanonicalIdentity | null>(null);
  const [fetchLoading, setFetchLoading] = useState(false);
  const [fetchError, setFetchError] = useState<Error | null>(null);
  const [selectedTenantId, setSelectedTenantId] = useState<string | null>(() =>
    readStoredTenantId(),
  );

  useEffect(() => {
    // Only fetch when authenticated — avoids an apiFetch /login bounce.
    if (!isAuthenticated) return;

    let cancelled = false;

    const loadIdentity = async () => {
      setFetchLoading(true);
      try {
        const res = await apiFetch("/api/identity");
        if (!res.ok) throw new Error(`identity fetch failed: ${res.status}`);
        const data = (await res.json()) as CanonicalIdentity;
        if (!cancelled) {
          setFetchedIdentity(data);
          setFetchError(null);
        }
      } catch (err: unknown) {
        if (cancelled) return;
        // Fail-closed — drop identity so no roles/tenants are surfaced.
        setFetchedIdentity(null);
        setFetchError(err instanceof Error ? err : new Error(String(err)));
      } finally {
        if (!cancelled) setFetchLoading(false);
      }
    };

    void loadIdentity();

    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, apiFetch]);

  // Fail-closed: never surface a stale identity/error once unauthenticated.
  const identity = isAuthenticated ? fetchedIdentity : null;
  const loading = isAuthenticated ? fetchLoading : false;
  const error = isAuthenticated ? fetchError : null;

  const currentTenantId = useMemo(() => {
    const memberships = identity?.tenant_memberships ?? [];
    if (memberships.length === 0) return null;
    const memberIds = new Set(memberships.map((m) => m.tenant_id));
    // Honor the client-side selection only while it remains a valid membership;
    // a stale selection falls back to the first membership.
    if (selectedTenantId && memberIds.has(selectedTenantId)) return selectedTenantId;
    return memberships[0].tenant_id;
  }, [identity, selectedTenantId]);

  const setCurrentTenantId = useCallback((tenantId: string) => {
    setSelectedTenantId(tenantId);
    try {
      window.localStorage.setItem(STORAGE_KEY, tenantId);
    } catch {
      // localStorage unavailable — selection persists for this session only.
    }
  }, []);

  const value = useMemo<IdentityContextValue>(
    () => ({ identity, loading, error, currentTenantId, setCurrentTenantId }),
    [identity, loading, error, currentTenantId, setCurrentTenantId],
  );

  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>;
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
