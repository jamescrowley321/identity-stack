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
 * - `currentTenantId`: the active tenant from IdentityContext: the token's
 *   tenant when it comes from the token, otherwise the client-side selection
 *   (defaulting to the first membership).
 * - `tenants`: every tenant the user belongs to. `tenant_memberships` is the
 *   source of id + display name; roles and permissions are aggregated across
 *   all of that tenant's role assignments (permissions de-duped). A tenant
 *   that comes from the token is always listed, with the token's permissions.
 */
export function useTenants() {
  const { identity, currentTenantId } = useIdentityContext();

  return useMemo(() => {
    if (!identity) return { currentTenantId: null, tenants: [] as TenantInfo[] };

    // Guard against a malformed-but-200 payload (gateway/cache/API-version
    // skew): a missing list must fail closed (empty) rather than throw
    // `undefined.map` in render, which — with no ErrorBoundary — would blank
    // the whole app tree.
    const memberships = identity.tenant_memberships ?? [];
    const allRoles = identity.roles ?? [];
    // A tenant that comes from the token: its permissions are the token's, and
    // the token grants nothing in any other membership.
    const tokenTenant = identity.token_tenant ?? null;

    const tenants: TenantInfo[] = memberships.map((membership) => {
      const tenantRoles = allRoles.filter(
        (r) => r.tenant_id === membership.tenant_id,
      );
      return {
        id: membership.tenant_id,
        name: membership.tenant_name || membership.tenant_id,
        roles: tenantRoles.map((r) => r.role_name),
        permissions: [
          ...new Set([
            ...tenantRoles.flatMap((r) => r.permissions ?? []),
            ...(tokenTenant?.tenant_id === membership.tenant_id
              ? (tokenTenant.permissions ?? [])
              : []),
          ]),
        ],
      };
    });

    // The API acts in the token's tenant even if the memberships do not list
    // it, so it is listed, with the token's permissions and no roles.
    if (tokenTenant !== null && !tenants.some((t) => t.id === tokenTenant.tenant_id)) {
      tenants.push({
        id: tokenTenant.tenant_id,
        name: tokenTenant.tenant_id,
        roles: [],
        permissions: [...new Set(tokenTenant.permissions ?? [])],
      });
    }

    return { currentTenantId, tenants };
  }, [identity, currentTenantId]);
}
