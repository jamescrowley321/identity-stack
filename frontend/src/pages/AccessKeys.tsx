import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { useApiClient } from "../hooks/useApiClient";

const EXPIRATION_OPTIONS = [
  { label: "Never", value: 0 },
  { label: "30 days", value: 30 },
  { label: "90 days", value: 90 },
  { label: "365 days", value: 365 },
];

interface AccessKey {
  id: string;
  name: string;
  status: string;
  createdTime?: number;
  expireTime?: number;
}

export default function AccessKeys() {
  const { apiFetch } = useApiClient();
  const [keys, setKeys] = useState<AccessKey[]>([]);
  const [name, setName] = useState("");
  const [expirationDays, setExpirationDays] = useState(0);
  const [newKeySecret, setNewKeySecret] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const loadKeys = useCallback(() => {
    apiFetch("/api/keys")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.keys) setKeys(data.keys);
      })
      .catch(() => {});
  }, [apiFetch]);

  useEffect(() => {
    loadKeys();
  }, [loadKeys]);

  const handleCreate = useCallback(async () => {
    if (!name.trim()) return;
    setStatus(null);
    setNewKeySecret(null);
    const body: Record<string, unknown> = { name: name.trim() };
    if (expirationDays > 0) {
      body.expire_time = Math.floor(Date.now() / 1000) + expirationDays * 86400;
    }
    try {
      const res = await apiFetch("/api/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        const data = await res.json();
        setNewKeySecret(data.cleartext || null);
        setName("");
        setCopied(false);
        loadKeys();
      } else {
        const err = await res.json();
        setStatus(`Error: ${err.detail || res.statusText}`);
      }
    } catch {
      setStatus("Failed to create key");
    }
  }, [name, expirationDays, apiFetch, loadKeys]);

  const handleDeactivate = useCallback(
    async (keyId: string) => {
      await apiFetch(`/api/keys/${keyId}/deactivate`, { method: "POST" });
      loadKeys();
    },
    [apiFetch, loadKeys],
  );

  const handleActivate = useCallback(
    async (keyId: string) => {
      await apiFetch(`/api/keys/${keyId}/activate`, { method: "POST" });
      loadKeys();
    },
    [apiFetch, loadKeys],
  );

  const handleDelete = useCallback(
    async (keyId: string) => {
      await apiFetch(`/api/keys/${keyId}`, { method: "DELETE" });
      loadKeys();
    },
    [apiFetch, loadKeys],
  );

  const copyToClipboard = useCallback(() => {
    if (newKeySecret) {
      navigator.clipboard.writeText(newKeySecret);
      setCopied(true);
    }
  }, [newKeySecret]);

  return (
    <div style={{ padding: "2rem", maxWidth: "900px", margin: "0 auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Access Keys</h1>
        <Link to="/">Back to Dashboard</Link>
      </header>

      <section style={{ marginTop: "2rem" }}>
        <h2>Create Key</h2>
        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
          <input
            type="text"
            placeholder="Key name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            style={{ padding: "0.25rem 0.5rem", minWidth: "200px" }}
          />
          <select
            value={expirationDays}
            onChange={(e) => setExpirationDays(Number(e.target.value))}
            style={{ padding: "0.25rem 0.5rem" }}
          >
            {EXPIRATION_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <button onClick={handleCreate} disabled={!name.trim()}>
            Create
          </button>
        </div>
        {status && <p style={{ marginTop: "0.5rem", color: "red" }}>{status}</p>}
      </section>

      {newKeySecret && (
        <section
          style={{
            marginTop: "1rem",
            padding: "1rem",
            background: "#fff8e1",
            border: "1px solid #ffc107",
            borderRadius: "4px",
          }}
        >
          <p>
            <strong>Key created!</strong> Copy the secret below — it will not be shown again.
          </p>
          <code style={{ display: "block", padding: "0.5rem", background: "#f5f5f5", borderRadius: "4px", wordBreak: "break-all" }}>
            {newKeySecret}
          </code>
          <button onClick={copyToClipboard} style={{ marginTop: "0.5rem" }}>
            {copied ? "Copied!" : "Copy to Clipboard"}
          </button>
        </section>
      )}

      <section style={{ marginTop: "2rem" }}>
        <h2>Keys</h2>
        {keys.length === 0 ? (
          <p style={{ color: "#666" }}>No access keys yet.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Name</th>
                <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Status</th>
                <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "2px solid #ddd" }}>ID</th>
                <th style={{ padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>{k.name}</td>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>
                    <span style={{ color: k.status === "active" ? "green" : "red" }}>{k.status}</span>
                  </td>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee", fontSize: "0.8rem", color: "#666" }}>
                    {k.id}
                  </td>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee", textAlign: "right" }}>
                    {k.status === "active" ? (
                      <button onClick={() => handleDeactivate(k.id)}>Revoke</button>
                    ) : (
                      <button onClick={() => handleActivate(k.id)}>Activate</button>
                    )}{" "}
                    <button onClick={() => handleDelete(k.id)} style={{ color: "red" }}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
