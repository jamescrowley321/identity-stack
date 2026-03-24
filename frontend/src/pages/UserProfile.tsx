import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { useApiClient } from "../hooks/useApiClient";

const USER_ATTRIBUTES = ["department", "job_title", "avatar_url"];

interface ProfileData {
  user_id: string;
  name: string;
  email: string;
  custom_attributes: Record<string, string>;
}

export default function UserProfile() {
  const { apiFetch } = useApiClient();
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [editKey, setEditKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [status, setStatus] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/api/profile")
      .then((res) => (res.ok ? res.json() : null))
      .then(setProfile)
      .catch(() => {});
  }, [apiFetch]);

  const handleSave = useCallback(async () => {
    if (!editKey) return;
    setStatus(null);
    try {
      const res = await apiFetch("/api/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: editKey, value: editValue }),
      });
      if (res.ok) {
        setProfile((prev) =>
          prev ? { ...prev, custom_attributes: { ...prev.custom_attributes, [editKey]: editValue } } : prev,
        );
        setEditKey(null);
        setStatus("Saved");
      } else {
        const err = await res.json();
        setStatus(`Error: ${err.detail || res.statusText}`);
      }
    } catch {
      setStatus("Failed to save");
    }
  }, [editKey, editValue, apiFetch]);

  if (!profile) return <div style={{ padding: "2rem" }}>Loading...</div>;

  return (
    <div style={{ padding: "2rem", maxWidth: "900px", margin: "0 auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>User Profile</h1>
        <Link to="/">Back to Dashboard</Link>
      </header>

      <section style={{ marginTop: "2rem" }}>
        <p><strong>Name:</strong> {profile.name || "Not set"}</p>
        <p><strong>Email:</strong> {profile.email || "Not set"}</p>
      </section>

      <section style={{ marginTop: "2rem" }}>
        <h2>Custom Attributes</h2>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "1px solid #ddd" }}>Attribute</th>
              <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "1px solid #ddd" }}>Value</th>
              <th style={{ padding: "0.5rem", borderBottom: "1px solid #ddd" }}></th>
            </tr>
          </thead>
          <tbody>
            {USER_ATTRIBUTES.map((key) => (
              <tr key={key}>
                <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>{key}</td>
                <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>
                  {editKey === key ? (
                    <input
                      type="text"
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      style={{ padding: "0.25rem", width: "100%" }}
                    />
                  ) : (
                    profile.custom_attributes[key] || <span style={{ color: "#999" }}>Not set</span>
                  )}
                </td>
                <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee", textAlign: "right" }}>
                  {editKey === key ? (
                    <>
                      <button onClick={handleSave}>Save</button>{" "}
                      <button onClick={() => setEditKey(null)}>Cancel</button>
                    </>
                  ) : (
                    <button onClick={() => { setEditKey(key); setEditValue(profile.custom_attributes[key] || ""); }}>
                      Edit
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {status && <p style={{ marginTop: "0.5rem", fontStyle: "italic" }}>{status}</p>}
      </section>
    </div>
  );
}
