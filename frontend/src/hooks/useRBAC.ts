import { useTenants } from "./useTenants";
import { useMemo, useCallback } from "react";

const EMPTY_ROLES: string[] = [];
const EMPTY_PERMISSIONS: string[] = [];

/**
 * RBAC hook for the current tenant context.
 *
 * Reads roles and permissions from the Descope JWT `tenants` claim
 * for the active tenant (`dct` claim).
 */
export function useRBAC() {
  const { currentTenantId, tenants } = useTenants();

  const currentTenant = useMemo(
    () => tenants.find((t) => t.id === currentTenantId),
    [currentTenantId, tenants],
  );

  const roles = useMemo(
    () => currentTenant?.roles ?? EMPTY_ROLES,
    [currentTenant?.roles],
  );
  const permissions = useMemo(
    () => currentTenant?.permissions ?? EMPTY_PERMISSIONS,
    [currentTenant?.permissions],
  );

  const hasRole = useCallback((role: string) => roles.includes(role), [roles]);
  const hasPermission = useCallback((perm: string) => permissions.includes(perm), [permissions]);

  const isOwner = useMemo(() => roles.includes("owner"), [roles]);
  const isAdmin = useMemo(() => roles.includes("admin") || roles.includes("owner"), [roles]);

  return { roles, permissions, hasRole, hasPermission, isOwner, isAdmin, currentTenantId };
}
