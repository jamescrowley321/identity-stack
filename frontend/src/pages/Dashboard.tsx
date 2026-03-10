import { useAuth } from "react-oidc-context";
import { useEffect, useState } from "react";

const preStyle = {
  background: "#f4f4f4",
  padding: "1rem",
  borderRadius: "4px",
  overflow: "auto" as const,
  maxHeight: "400px",
  fontSize: "0.85rem",
};

const labelStyle = { fontSize: "0.75rem", color: "#666", fontWeight: "normal" as const };

export default function Dashboard() {
  const auth = useAuth();
  const [health, setHealth] = useState<string>("checking...");
  const [accessTokenClaims, setAccessTokenClaims] = useState<Record<string, unknown> | null>(null);
  const [idTokenClaims, setIdTokenClaims] = useState<Record<string, unknown> | null>(null);
  const [identity, setIdentity] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const accessToken = auth.user?.access_token;
  const idToken = auth.user?.id_token;

  useEffect(() => {
    fetch("/api/health")
      .then((res) => res.json())
      .then((data) => setHealth(data.status))
      .catch(() => setHealth("unreachable"));
  }, []);

  useEffect(() => {
    if (!accessToken) return;

    const headers = { Authorization: `Bearer ${accessToken}` };

    fetch("/api/claims", { headers })
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then(setAccessTokenClaims)
      .catch((err) => setError(err.message));

    fetch("/api/me", { headers })
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then(setIdentity)
      .catch(() => {});
  }, [accessToken]);

  useEffect(() => {
    if (!idToken || !accessToken) return;

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
  }, [idToken, accessToken]);

  const identityName = identity?.identity as Record<string, unknown> | undefined;

  return (
    <div style={{ padding: "2rem", maxWidth: "900px", margin: "0 auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Descope SaaS Starter</h1>
        <button onClick={() => auth.signoutRedirect()}>Logout</button>
      </header>
      <section style={{ marginTop: "2rem" }}>
        <h2>Welcome, {(identityName?.name as string) || auth.user?.profile?.email || "User"}</h2>
        <p>Backend status: <strong>{health}</strong></p>
      </section>

      {error && (
        <section style={{ marginTop: "2rem" }}>
          <pre style={{ ...preStyle, background: "#fff0f0", color: "red" }}>{error}</pre>
        </section>
      )}

      <section style={{ marginTop: "2rem" }}>
        <h3>ClaimsIdentity <span style={labelStyle}>(py-identity-model ClaimsPrincipal)</span></h3>
        <pre style={preStyle}>
          {identity ? JSON.stringify(identity, null, 2) : "Loading..."}
        </pre>
      </section>

      <section style={{ marginTop: "2rem" }}>
        <h3>Access Token Claims <span style={labelStyle}>(validated by py-identity-model)</span></h3>
        <pre style={preStyle}>
          {accessTokenClaims ? JSON.stringify(accessTokenClaims, null, 2) : "Loading..."}
        </pre>
      </section>

      <section style={{ marginTop: "2rem" }}>
        <h3>ID Token Claims <span style={labelStyle}>(validated by py-identity-model)</span></h3>
        <pre style={preStyle}>
          {idTokenClaims ? JSON.stringify(idTokenClaims, null, 2) : "Loading..."}
        </pre>
      </section>
    </div>
  );
}
