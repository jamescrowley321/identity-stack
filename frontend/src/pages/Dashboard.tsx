import { useAuth } from "react-oidc-context";
import { useEffect, useState } from "react";

export default function Dashboard() {
  const auth = useAuth();
  const [health, setHealth] = useState<string>("checking...");

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
    <div style={{ padding: "2rem" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Descope SaaS Starter</h1>
        <button onClick={() => auth.signoutRedirect()}>Logout</button>
      </header>
      <section style={{ marginTop: "2rem" }}>
        <h2>Welcome, {auth.user?.profile?.name || auth.user?.profile?.email || "User"}</h2>
        <p>Backend status: <strong>{health}</strong></p>
      </section>
    </div>
  );
}
