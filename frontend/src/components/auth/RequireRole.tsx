import { useRBAC } from "../../hooks/useRBAC";

interface RequireRoleProps {
  role: string;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

/**
 * Conditionally renders children only if the user has the specified role
 * in the current tenant.
 */
export function RequireRole({ role, children, fallback = null }: RequireRoleProps) {
  const { hasRole } = useRBAC();
  return hasRole(role) ? <>{children}</> : <>{fallback}</>;
}
