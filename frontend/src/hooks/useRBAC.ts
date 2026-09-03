import { useTenants } from "./useTenants";
import { useMemo, useCallback } from "react";

const EMPTY_ROLES: string[] = [];
const EMPTY_PERMISSIONS: string[] = [];

/**
 * RBAC hook for the current tenant context.
 *
 * Reads roles and permissions from the canonical `GET /api/identity` payload
 * (via useTenants) for the active tenant. Provider-neutral — the public API
 * ({roles, permissions, hasRole, hasPermission, isOwner, isAdmin,
 * currentTenantId}) is unchanged regardless of the upstream IdP.
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
