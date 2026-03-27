import { useAuth } from "react-oidc-context";
import { useEffect, useState, useCallback } from "react";
import { useApiClient } from "../hooks/useApiClient";
import { useTenants } from "../hooks/useTenants";
import { useRBAC } from "../hooks/useRBAC";
import { RequirePermission } from "../components/auth/RequirePermission";
import { PageHeader } from "../components/layout/PageHeader";

const preStyle = {
  background: "#f4f4f4",
  padding: "1rem",
  borderRadius: "4px",
  overflow: "auto" as const,
  maxHeight: "400px",
  fontSize: "0.85rem",
};

const labelStyle = { fontSize: "0.75rem", color: "#666", fontWeight: "normal" as const };

interface TenantResource {
  id: string;
  tenant_id: string;
  name: string;
  description: string;
  created_at: string;
}

export default function Dashboard() {
  const auth = useAuth();
  const { apiFetch } = useApiClient();
  const { currentTenantId, tenants } = useTenants();
  const { roles } = useRBAC();
  const [health, setHealth] = useState<string>("checking...");
  const [accessTokenClaims, setAccessTokenClaims] = useState<Record<string, unknown> | null>(null);
  const [idTokenClaims, setIdTokenClaims] = useState<Record<string, unknown> | null>(null);
  const [identity, setIdentity] = useState<Record<string, unknown> | null>(null);
  const [resources, setResources] = useState<TenantResource[]>([]);
  const [newResourceName, setNewResourceName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const idToken = auth.user?.id_token;

  useEffect(() => {
    fetch("/api/health")
      .then((res) => res.json())
      .then((data) => setHealth(data.status))
      .catch(() => setHealth("unreachable"));
  }, []);

  useEffect(() => {
    if (!auth.user?.access_token) return;

    apiFetch("/api/claims")
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then(setAccessTokenClaims)
      .catch((err) => setError(err.message));

    apiFetch("/api/me")
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then(setIdentity)
      .catch(() => {});
  }, [auth.user?.access_token, apiFetch]);

  useEffect(() => {
    if (!idToken || !auth.user?.access_token) return;

    fetch("/api/validate-id-token", {
      method: "POST",
      headers: { Authorization: `Bearer ${idToken}` },
    })
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then(setIdTokenClaims)
      .catch(() => {});
  }, [idToken, auth.user?.access_token]);

  const loadResources = useCallback(() => {
    if (!currentTenantId || !auth.user?.access_token) return;
    apiFetch(`/api/tenants/${currentTenantId}/resources`)
      .then((res) => {
        if (!res.ok) return;
        return res.json();
      })
      .then((data) => {
        if (data) setResources(data.resources);
      })
      .catch(() => {});
  }, [currentTenantId, auth.user?.access_token, apiFetch]);

  useEffect(() => {
    loadResources();
  }, [loadResources]);

  const handleCreateResource = useCallback(async () => {
    if (!currentTenantId || !newResourceName.trim()) return;
    try {
      const res = await apiFetch(`/api/tenants/${currentTenantId}/resources`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newResourceName.trim(), description: "" }),
      });
      if (res.ok) {
        setNewResourceName("");
        loadResources();
      }
    } catch {
      // Silently handle — error shown elsewhere if needed
    }
  }, [currentTenantId, newResourceName, apiFetch, loadResources]);

  const identityName = identity?.identity as Record<string, unknown> | undefined;

  return (
    <>
      <PageHeader
        title={`Welcome, ${(identityName?.name as string) || auth.user?.profile?.email || "User"}`}
        description={`Backend: ${health} · Tenant: ${currentTenantId ?? "none"} · Roles: ${roles.join(", ") || "none"}`}
      />
      <div className="p-6 space-y-6">
        {tenants.length > 0 && (
          <p style={{ fontSize: "0.9rem" }}>
            Tenant memberships: <strong>{tenants.map((t) => t.id).join(", ")}</strong>
          </p>
        )}

        {error && (
          <pre style={{ ...preStyle, background: "#fff0f0", color: "red" }}>{error}</pre>
        )}

        {currentTenantId && (
          <section>
            <h3>Tenant Resources <span style={labelStyle}>(scoped to {currentTenantId})</span></h3>
            <RequirePermission permission="documents.write">
              <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.5rem" }}>
                <input
                  type="text"
                  placeholder="Resource name"
                  value={newResourceName}
                  onChange={(e) => setNewResourceName(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleCreateResource()}
                  style={{ padding: "0.25rem 0.5rem", flex: 1 }}
                />
                <button onClick={handleCreateResource} disabled={!newResourceName.trim()}>
                  Create
                </button>
              </div>
            </RequirePermission>
            {resources.length === 0 ? (
              <p style={{ color: "#666", fontSize: "0.9rem" }}>No resources yet. Create one above.</p>
            ) : (
              <ul style={{ listStyle: "none", padding: 0 }}>
                {resources.map((r) => (
                  <li key={r.id} style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>
                    <strong>{r.name}</strong>
                    <span style={{ ...labelStyle, marginLeft: "0.5rem" }}>{r.id}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        <section>
          <h3>ClaimsIdentity <span style={labelStyle}>(py-identity-model ClaimsPrincipal)</span></h3>
          <pre style={preStyle}>
            {identity ? JSON.stringify(identity, null, 2) : "Loading..."}
          </pre>
        </section>

        <section>
          <h3>Access Token Claims <span style={labelStyle}>(validated by py-identity-model)</span></h3>
          <pre style={preStyle}>
            {accessTokenClaims ? JSON.stringify(accessTokenClaims, null, 2) : "Loading..."}
          </pre>
        </section>

        <section>
          <h3>ID Token Claims <span style={labelStyle}>(validated by py-identity-model)</span></h3>
          <pre style={preStyle}>
            {idTokenClaims ? JSON.stringify(idTokenClaims, null, 2) : "Loading..."}
          </pre>
        </section>
      </div>
    </>
  );
}
