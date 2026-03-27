import { useAuth } from "react-oidc-context";
import { useMemo } from "react";
import { jwtDecode } from "../utils/jwt";

export interface TenantInfo {
  id: string;
  roles: string[];
  permissions: string[];
}

interface DescopeClaims {
  dct?: string;
  tenants?: Record<string, { roles?: string[]; permissions?: string[] }>;
}

/**
 * Extract tenant information from the Descope access token.
 *
 * - `currentTenantId`: the `dct` claim (current tenant context)
 * - `tenants`: all tenants the user belongs to with their roles/permissions
 */
export function useTenants() {
  const auth = useAuth();

  return useMemo(() => {
    const token = auth.user?.access_token;
    if (!token) return { currentTenantId: null, tenants: [] };

    try {
      const claims = jwtDecode<DescopeClaims>(token);
      const currentTenantId = claims.dct ?? null;
      const tenantsMap = claims.tenants ?? {};
      const tenants: TenantInfo[] = Object.entries(tenantsMap).map(([id, info]) => ({
        id,
        roles: info.roles ?? [],
        permissions: info.permissions ?? [],
      }));
      return { currentTenantId, tenants };
    } catch {
      return { currentTenantId: null, tenants: [] };
    }
  }, [auth.user?.access_token]);
}
