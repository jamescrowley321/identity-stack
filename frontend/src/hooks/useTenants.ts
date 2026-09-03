import { useMemo } from "react";
import { useIdentityContext } from "../contexts/IdentityContext";

export interface TenantInfo {
  id: string;
  name: string;
  roles: string[];
  permissions: string[];
}

/**
 * Tenant memberships and the active tenant, sourced from the canonical
 * `GET /api/identity` payload (provider-neutral — no Descope `dct`/`tenants`
 * claim decoding).
 *
 * - `currentTenantId`: the active tenant (client-side selection from
 *   IdentityContext; defaults to the first membership).
 * - `tenants`: every tenant the user belongs to. `tenant_memberships` is the
 *   source of id + display name; roles and permissions are aggregated across
 *   all of that tenant's role assignments (permissions de-duped).
 */
export function useTenants() {
  const { identity, currentTenantId } = useIdentityContext();

  return useMemo(() => {
    if (!identity) return { currentTenantId: null, tenants: [] as TenantInfo[] };

    const tenants: TenantInfo[] = identity.tenant_memberships.map((membership) => {
      const tenantRoles = identity.roles.filter(
        (r) => r.tenant_id === membership.tenant_id,
      );
      return {
        id: membership.tenant_id,
        name: membership.tenant_name || membership.tenant_id,
        roles: tenantRoles.map((r) => r.role_name),
        permissions: [...new Set(tenantRoles.flatMap((r) => r.permissions))],
      };
    });

    return { currentTenantId, tenants };
  }, [identity, currentTenantId]);
}
