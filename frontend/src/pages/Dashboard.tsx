import { useAuth } from "react-oidc-context";
import { useEffect, useMemo, useState } from "react";

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const payload = atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(payload);
  } catch {
    return null;
  }
}

export default function Dashboard() {
  const auth = useAuth();
  const [health, setHealth] = useState<string>("checking...");

  const accessTokenClaims = useMemo(
    () => (auth.user?.access_token ? decodeJwtPayload(auth.user.access_token) : null),
    [auth.user?.access_token],
  );

  const idTokenClaims = useMemo(
    () => (auth.user?.id_token ? decodeJwtPayload(auth.user.id_token) : null),
    [auth.user?.id_token],
  );

  useEffect(() => {
    const token = auth.user?.access_token;
    fetch("/api/health", {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then((res) => res.json())
      .then((data) => setHealth(data.status))
      .catch(() => setHealth("unreachable"));
  }, [auth.user?.access_token]);

  return (
    <div style={{ padding: "2rem", maxWidth: "900px", margin: "0 auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Descope SaaS Starter</h1>
        <button onClick={() => auth.signoutRedirect()}>Logout</button>
      </header>
      <section style={{ marginTop: "2rem" }}>
        <h2>Welcome, {auth.user?.profile?.name || auth.user?.profile?.email || "User"}</h2>
        <p>Backend status: <strong>{health}</strong></p>
      </section>
      <section style={{ marginTop: "2rem" }}>
        <h3>Access Token Claims</h3>
        <pre style={{ background: "#f4f4f4", padding: "1rem", borderRadius: "4px", overflow: "auto", maxHeight: "400px", fontSize: "0.85rem" }}>
          {accessTokenClaims ? JSON.stringify(accessTokenClaims, null, 2) : "No access token"}
        </pre>
      </section>
      <section style={{ marginTop: "2rem" }}>
        <h3>ID Token Claims</h3>
        <pre style={{ background: "#f4f4f4", padding: "1rem", borderRadius: "4px", overflow: "auto", maxHeight: "400px", fontSize: "0.85rem" }}>
          {idTokenClaims ? JSON.stringify(idTokenClaims, null, 2) : "No ID token"}
        </pre>
      </section>
    </div>
  );
}
