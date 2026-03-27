import { useEffect, useState, useCallback } from "react";
import { useApiClient } from "../hooks/useApiClient";
import { useRBAC } from "../hooks/useRBAC";
import { Link } from "react-router-dom";

const AVAILABLE_ROLES = ["owner", "admin", "member", "viewer"];

interface RoleHierarchyEntry {
  description: string;
  permissions: string[];
}

export default function RoleManagement() {
  const { apiFetch } = useApiClient();
  const { roles, permissions, isAdmin, currentTenantId } = useRBAC();
  const [userId, setUserId] = useState("");
  const [selectedRole, setSelectedRole] = useState(AVAILABLE_ROLES[2]);
  const [status, setStatus] = useState<string | null>(null);

  const [myRoles, setMyRoles] = useState<{ roles: string[]; permissions: string[] } | null>(null);
  const [hierarchy, setHierarchy] = useState<Record<string, RoleHierarchyEntry> | null>(null);

  useEffect(() => {
    apiFetch("/api/roles/me")
      .then((res) => (res.ok ? res.json() : null))
      .then(setMyRoles)
      .catch(() => {});
    apiFetch("/api/rbac/hierarchy")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => data && setHierarchy(data.roles))
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
    <div style={{ padding: "2rem", maxWidth: "900px", margin: "0 auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Role Management</h1>
        <Link to="/">Back to Dashboard</Link>
      </header>

      <section style={{ marginTop: "2rem" }}>
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
        <section style={{ marginTop: "2rem" }}>
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
        <p style={{ marginTop: "2rem", color: "#666" }}>
          You need an admin or owner role to manage other users' roles.
        </p>
      )}

      <section style={{ marginTop: "2rem" }}>
        <h2>Effective Permissions</h2>
        <p style={{ fontSize: "0.85rem", color: "#666" }}>
          These are the permissions Descope resolved for your current session via JWT claims.
        </p>
        {permissions.length > 0 ? (
          <ul style={{ columns: 2, listStyle: "none", padding: 0, margin: "0.5rem 0" }}>
            {permissions.map((p) => (
              <li key={p} style={{ padding: "0.15rem 0", fontFamily: "monospace", fontSize: "0.9rem" }}>
                {p}
              </li>
            ))}
          </ul>
        ) : (
          <p>No permissions resolved.</p>
        )}
      </section>

      {hierarchy && (
        <section style={{ marginTop: "2rem" }}>
          <h2>Role Hierarchy</h2>
          <p style={{ fontSize: "0.85rem", color: "#666" }}>
            Each higher role is a superset of the one below it.
          </p>
          <table style={{ borderCollapse: "collapse", width: "100%", marginTop: "0.5rem" }}>
            <thead>
              <tr style={{ borderBottom: "2px solid #ccc", textAlign: "left" }}>
                <th style={{ padding: "0.4rem 0.6rem" }}>Role</th>
                <th style={{ padding: "0.4rem 0.6rem" }}>Description</th>
                <th style={{ padding: "0.4rem 0.6rem" }}>Permissions</th>
              </tr>
            </thead>
            <tbody>
              {AVAILABLE_ROLES.map((role) => {
                const entry = hierarchy[role];
                if (!entry) return null;
                const isMyRole = roles.includes(role);
                return (
                  <tr key={role} style={{ borderBottom: "1px solid #eee", background: isMyRole ? "#e8f5e9" : undefined }}>
                    <td style={{ padding: "0.4rem 0.6rem", fontWeight: "bold" }}>
                      {role}{isMyRole ? " *" : ""}
                    </td>
                    <td style={{ padding: "0.4rem 0.6rem", fontSize: "0.9rem" }}>{entry.description}</td>
                    <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace", fontSize: "0.8rem" }}>
                      {entry.permissions.join(", ")}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p style={{ fontSize: "0.8rem", color: "#666", marginTop: "0.3rem" }}>
            * = your current role
          </p>
        </section>
      )}
    </div>
  );
}
