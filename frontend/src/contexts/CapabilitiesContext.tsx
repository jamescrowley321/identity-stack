import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useAuth } from "react-oidc-context";
import { useApiClient } from "../hooks/useApiClient";
import {
  NO_CAPABILITIES,
  parseCapabilities,
  servedBy,
  type Capabilities,
  type Requirement,
} from "../lib/capabilities";

interface CapabilitiesContextValue extends Capabilities {
  loading: boolean;
  /** Whether the deployment serves what `requirement` names. */
  serves: (requirement: Requirement) => boolean;
}

const CapabilitiesContext = createContext<CapabilitiesContextValue | null>(null);

/**
 * Loads `GET /api/capabilities` once per signed-in subject. Fails closed: until
 * it answers, and when it fails, the deployment serves nothing optional.
 */
export function CapabilitiesProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const subject = auth.user?.profile?.sub ?? null;
  const { apiFetch } = useApiClient();
  // The capabilities are the deployment's, not the tenant's: a tenant switch,
  // which changes apiFetch, does not load them again.
  const apiFetchRef = useRef(apiFetch);
  useEffect(() => {
    apiFetchRef.current = apiFetch;
  }, [apiFetch]);

  const [loaded, setLoaded] = useState<{ subject: string | null; capabilities: Capabilities } | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiFetchRef
      .current("/api/capabilities")
      .then((res) => (res.ok ? res.json() : null))
      .then((body: unknown) => parseCapabilities(body))
      .catch(() => NO_CAPABILITIES)
      .then((capabilities) => {
        if (!cancelled) setLoaded({ subject, capabilities });
      });
    return () => {
      cancelled = true;
    };
  }, [subject]);

  const current = loaded?.subject === subject ? loaded.capabilities : null;
  const value = useMemo<CapabilitiesContextValue>(() => {
    const capabilities = current ?? NO_CAPABILITIES;
    return {
      ...capabilities,
      loading: current === null,
      serves: (requirement) => servedBy(capabilities, requirement),
    };
  }, [current]);

  return <CapabilitiesContext.Provider value={value}>{children}</CapabilitiesContext.Provider>;
}

/** The deployment's capabilities. Throws outside a `<CapabilitiesProvider>`. */
export function useCapabilities(): CapabilitiesContextValue {
  const ctx = useContext(CapabilitiesContext);
  if (ctx === null) {
    throw new Error("useCapabilities must be used within a CapabilitiesProvider");
  }
  return ctx;
}
