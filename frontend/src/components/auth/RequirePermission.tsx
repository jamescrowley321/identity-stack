import { useRBAC } from "../../hooks/useRBAC";

interface RequirePermissionProps {
  permission: string;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

/**
 * Conditionally renders children only if the user has the specified permission
 * in the current tenant.
 */
export function RequirePermission({ permission, children, fallback = null }: RequirePermissionProps) {
  const { hasPermission } = useRBAC();
  return hasPermission(permission) ? <>{children}</> : <>{fallback}</>;
}
