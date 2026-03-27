import { useEffect, useState, useCallback } from "react";
import { useApiClient } from "../hooks/useApiClient";
import { useRBAC } from "../hooks/useRBAC";
import { PageHeader } from "../components/layout/PageHeader";

interface TenantSettingsData {
  tenant_id: string;
  name: string;
  custom_attributes: Record<string, string | number | boolean>;
}

const PLAN_TIERS = ["free", "pro", "enterprise"];

export default function TenantSettings() {
  const { apiFetch } = useApiClient();
  const { isAdmin } = useRBAC();
  const [settings, setSettings] = useState<TenantSettingsData | null>(null);
  const [planTier, setPlanTier] = useState("free");
  const [maxMembers, setMaxMembers] = useState("10");
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/api/tenants/current/settings")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data) {
          setSettings(data);
          setPlanTier(String(data.custom_attributes?.plan_tier ?? "free"));
          setMaxMembers(String(data.custom_attributes?.max_members ?? "10"));
        }
      })
      .catch(() => {});
  }, [apiFetch]);

  const handleSave = useCallback(async () => {
    setStatus(null);
    try {
      const res = await apiFetch("/api/tenants/current/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          custom_attributes: { plan_tier: planTier, max_members: parseInt(maxMembers, 10) || 10 },
        }),
      });
      if (res.ok) {
        setStatus("Settings saved");
        setSettings((prev) =>
          prev
            ? { ...prev, custom_attributes: { ...prev.custom_attributes, plan_tier: planTier, max_members: parseInt(maxMembers, 10) || 10 } }
            : prev,
        );
      } else {
        const err = await res.json();
        setStatus(`Error: ${err.detail || res.statusText}`);
      }
    } catch {
      setStatus("Failed to save");
    }
  }, [planTier, maxMembers, apiFetch]);

  if (!settings) {
    return (
      <div className="flex items-center justify-center p-8">
        <p className="text-muted-foreground">Loading...</p>
      </div>
    );
  }

  const attrs = settings.custom_attributes;

  return (
    <>
      <PageHeader title="Tenant Settings" description={settings.name || settings.tenant_id} />
      <div className="p-6 space-y-6">
        <section>
          <p><strong>Plan:</strong> {String(attrs.plan_tier ?? "free")}</p>
          <p><strong>Max Members:</strong> {String(attrs.max_members ?? "Not set")}</p>
        </section>

        {isAdmin && (
          <section>
            <h2>Edit Settings</h2>
            <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginTop: "0.5rem" }}>
              <label>
                Plan Tier:{" "}
                <select value={planTier} onChange={(e) => setPlanTier(e.target.value)}>
                  {PLAN_TIERS.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </label>
              <label>
                Max Members:{" "}
                <input
                  type="number"
                  value={maxMembers}
                  onChange={(e) => setMaxMembers(e.target.value)}
                  style={{ width: "80px", padding: "0.25rem" }}
                  min="1"
                />
              </label>
              <button onClick={handleSave}>Save</button>
            </div>
            {status && <p style={{ marginTop: "0.5rem", fontStyle: "italic" }}>{status}</p>}
          </section>
        )}

        {!isAdmin && (
          <p style={{ color: "#666" }}>
            Contact an admin to change tenant settings.
          </p>
        )}
      </div>
    </>
  );
}
