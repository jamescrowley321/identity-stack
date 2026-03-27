import { useAuth } from "react-oidc-context";
import { useTenants, TenantInfo } from "../../hooks/useTenants";

const containerStyle = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
};

const selectStyle = {
  padding: "0.25rem 0.5rem",
  borderRadius: "4px",
  border: "1px solid #ccc",
  fontSize: "0.85rem",
};

const badgeStyle = {
  padding: "0.25rem 0.75rem",
  borderRadius: "12px",
  background: "#e8f0fe",
  color: "#1a73e8",
  fontSize: "0.8rem",
  fontWeight: "500" as const,
};

/**
 * Displays the user's current tenant and allows switching between tenants.
 *
 * Switching tenants triggers a new sign-in with the `tenant` parameter,
 * which tells Descope to issue tokens scoped to the selected tenant.
 */
export default function TenantSwitcher() {
  const auth = useAuth();
  const { currentTenantId, tenants } = useTenants();

  if (tenants.length === 0) {
    return <span style={{ fontSize: "0.85rem", color: "#666" }}>No tenants</span>;
  }

  const handleSwitch = (tenantId: string) => {
    if (tenantId === currentTenantId) return;
    // Re-authenticate with the selected tenant context.
    // Descope uses the `tenant` parameter in the auth request to scope the session.
    auth.signinRedirect({ extraQueryParams: { tenant: tenantId } });
  };

  if (tenants.length === 1) {
    return <span style={badgeStyle}>{tenants[0].id}</span>;
  }

  return (
    <div style={containerStyle}>
      <label htmlFor="tenant-select" style={{ fontSize: "0.85rem", color: "#666" }}>
        Tenant:
      </label>
      <select
        id="tenant-select"
        style={selectStyle}
        value={currentTenantId ?? ""}
        onChange={(e) => handleSwitch(e.target.value)}
      >
        {!currentTenantId && <option value="">Select tenant...</option>}
        {tenants.map((t: TenantInfo) => (
          <option key={t.id} value={t.id}>
            {t.id}
          </option>
        ))}
      </select>
    </div>
  );
}
