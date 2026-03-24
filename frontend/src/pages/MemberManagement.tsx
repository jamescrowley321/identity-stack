import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { useApiClient } from "../hooks/useApiClient";

const AVAILABLE_ROLES = ["owner", "admin", "member", "viewer"];

interface Member {
  userId: string;
  name?: string;
  email?: string;
  status?: string;
  roleNames?: string[];
}

export default function MemberManagement() {
  const { apiFetch } = useApiClient();
  const [members, setMembers] = useState<Member[]>([]);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");
  const [status, setStatus] = useState<string | null>(null);

  const loadMembers = useCallback(() => {
    apiFetch("/api/members")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.members) setMembers(data.members);
      })
      .catch(() => {});
  }, [apiFetch]);

  useEffect(() => {
    loadMembers();
  }, [loadMembers]);

  const handleInvite = useCallback(async () => {
    if (!inviteEmail.trim()) return;
    setStatus(null);
    try {
      const res = await apiFetch("/api/members/invite", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: inviteEmail.trim(), role_names: [inviteRole] }),
      });
      if (res.ok) {
        setStatus(`Invited ${inviteEmail.trim()}`);
        setInviteEmail("");
        loadMembers();
      } else {
        const err = await res.json();
        setStatus(`Error: ${err.detail || res.statusText}`);
      }
    } catch {
      setStatus("Failed to invite");
    }
  }, [inviteEmail, inviteRole, apiFetch, loadMembers]);

  const handleToggleStatus = useCallback(
    async (userId: string, currentStatus: string) => {
      const action = currentStatus === "enabled" ? "deactivate" : "activate";
      await apiFetch(`/api/members/${userId}/${action}`, { method: "POST" });
      loadMembers();
    },
    [apiFetch, loadMembers],
  );

  const handleRemove = useCallback(
    async (userId: string) => {
      await apiFetch(`/api/members/${userId}`, { method: "DELETE" });
      loadMembers();
    },
    [apiFetch, loadMembers],
  );

  return (
    <div style={{ padding: "2rem", maxWidth: "900px", margin: "0 auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Members</h1>
        <Link to="/">Back to Dashboard</Link>
      </header>

      <section style={{ marginTop: "2rem" }}>
        <h2>Invite Member</h2>
        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
          <input
            type="email"
            placeholder="Email address"
            value={inviteEmail}
            onChange={(e) => setInviteEmail(e.target.value)}
            style={{ padding: "0.25rem 0.5rem", minWidth: "250px" }}
          />
          <select value={inviteRole} onChange={(e) => setInviteRole(e.target.value)} style={{ padding: "0.25rem 0.5rem" }}>
            {AVAILABLE_ROLES.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
          <button onClick={handleInvite} disabled={!inviteEmail.trim()}>Invite</button>
        </div>
        {status && <p style={{ marginTop: "0.5rem", fontStyle: "italic" }}>{status}</p>}
      </section>

      <section style={{ marginTop: "2rem" }}>
        <h2>Team ({members.length})</h2>
        {members.length === 0 ? (
          <p style={{ color: "#666" }}>No members found.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Name / Email</th>
                <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Roles</th>
                <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Status</th>
                <th style={{ padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.userId}>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>
                    <strong>{m.name || m.email || m.userId}</strong>
                    {m.email && m.name && <div style={{ fontSize: "0.8rem", color: "#666" }}>{m.email}</div>}
                  </td>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>
                    {(m.roleNames || []).map((r) => (
                      <span
                        key={r}
                        style={{
                          display: "inline-block",
                          padding: "0.1rem 0.4rem",
                          margin: "0.1rem",
                          borderRadius: "8px",
                          background: r === "owner" ? "#e8f5e9" : r === "admin" ? "#e3f2fd" : "#f5f5f5",
                          fontSize: "0.8rem",
                        }}
                      >
                        {r}
                      </span>
                    ))}
                  </td>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>
                    <span style={{ color: m.status === "enabled" ? "green" : "red" }}>
                      {m.status || "unknown"}
                    </span>
                  </td>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee", textAlign: "right" }}>
                    <button onClick={() => handleToggleStatus(m.userId, m.status || "enabled")}>
                      {m.status === "enabled" ? "Deactivate" : "Activate"}
                    </button>{" "}
                    <button onClick={() => handleRemove(m.userId)} style={{ color: "red" }}>
                      Remove
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
