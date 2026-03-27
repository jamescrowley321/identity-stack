import { useEffect, useState, useCallback } from "react";
import { useApiClient } from "../hooks/useApiClient";
import { useRBAC } from "../hooks/useRBAC";
import { PageHeader } from "../components/layout/PageHeader";

const AVAILABLE_ROLES = ["owner", "admin", "member", "viewer"];

export default function RoleManagement() {
  const { apiFetch } = useApiClient();
  const { roles, permissions, isAdmin, currentTenantId } = useRBAC();
  const [userId, setUserId] = useState("");
  const [selectedRole, setSelectedRole] = useState(AVAILABLE_ROLES[2]);
  const [status, setStatus] = useState<string | null>(null);

  const [myRoles, setMyRoles] = useState<{ roles: string[]; permissions: string[] } | null>(null);

  useEffect(() => {
    apiFetch("/api/roles/me")
      .then((res) => (res.ok ? res.json() : null))
      .then(setMyRoles)
      .catch(() => {});
  }, [apiFetch]);

  const handleAssign = useCallback(async () => {
    if (!userId.trim() || !currentTenantId) return;
    setStatus(null);
    try {
      const res = await apiFetch("/api/roles/assign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId.trim(), tenant_id: currentTenantId, role_names: [selectedRole] }),
      });
      if (res.ok) {
        setStatus(`Assigned "${selectedRole}" to ${userId.trim()}`);
        setUserId("");
      } else {
        const err = await res.json();
        setStatus(`Error: ${err.detail || res.statusText}`);
      }
    } catch {
      setStatus("Failed to assign role");
    }
  }, [userId, selectedRole, currentTenantId, apiFetch]);

  const handleRemove = useCallback(async () => {
    if (!userId.trim() || !currentTenantId) return;
    setStatus(null);
    try {
      const res = await apiFetch("/api/roles/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId.trim(), tenant_id: currentTenantId, role_names: [selectedRole] }),
      });
      if (res.ok) {
        setStatus(`Removed "${selectedRole}" from ${userId.trim()}`);
        setUserId("");
      } else {
        const err = await res.json();
        setStatus(`Error: ${err.detail || res.statusText}`);
      }
    } catch {
      setStatus("Failed to remove role");
    }
  }, [userId, selectedRole, currentTenantId, apiFetch]);

  return (
    <>
      <PageHeader title="Role Management" description="View and manage user roles" />
      <div className="p-6 space-y-6">
        <section>
          <h2>Your Roles</h2>
          <p>
            <strong>Roles:</strong> {roles.length > 0 ? roles.join(", ") : "None"}
          </p>
          <p>
            <strong>Permissions:</strong> {permissions.length > 0 ? permissions.join(", ") : "None"}
          </p>
          {myRoles && (
            <p style={{ fontSize: "0.85rem", color: "#666" }}>
              (Server-confirmed: {myRoles.roles.join(", ") || "none"})
            </p>
          )}
        </section>

        {isAdmin && (
          <section>
            <h2>Manage User Roles</h2>
            <p style={{ fontSize: "0.85rem", color: "#666" }}>
              Assign or remove roles for users in tenant <strong>{currentTenantId}</strong>.
            </p>
            <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem", flexWrap: "wrap" }}>
              <input
                type="text"
                placeholder="User ID (login ID)"
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
                style={{ padding: "0.25rem 0.5rem", minWidth: "200px" }}
              />
              <select value={selectedRole} onChange={(e) => setSelectedRole(e.target.value)} style={{ padding: "0.25rem 0.5rem" }}>
                {AVAILABLE_ROLES.map((r) => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
              <button onClick={handleAssign} disabled={!userId.trim()}>Assign</button>
              <button onClick={handleRemove} disabled={!userId.trim()}>Remove</button>
            </div>
            {status && <p style={{ marginTop: "0.5rem", fontStyle: "italic" }}>{status}</p>}
          </section>
        )}

        {!isAdmin && (
          <p style={{ color: "#666" }}>
            You need an admin or owner role to manage other users' roles.
          </p>
        )}
      </div>
    </>
  );
}
