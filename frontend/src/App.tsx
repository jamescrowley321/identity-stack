import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "react-oidc-context";
import Dashboard from "./pages/Dashboard";

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const auth = useAuth();

  // Show loading during initial load AND during code exchange
  if (auth.isLoading) {
    return <div style={{ padding: "2rem" }}>Loading...</div>;
  }

  if (auth.error) {
    return (
      <div style={{ padding: "2rem" }}>
        <h2>Authentication Error</h2>
        <pre>{auth.error.message}</pre>
        <button onClick={() => auth.signinRedirect()}>Try Again</button>
      </div>
    );
  }

  if (!auth.isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

function Login() {
  const auth = useAuth();

  if (auth.isLoading) {
    return <div style={{ padding: "2rem" }}>Loading...</div>;
  }

  if (auth.isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  return (
    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", flexDirection: "column", gap: "1rem" }}>
      <h1>Descope SaaS Starter</h1>
      {auth.error && <p style={{ color: "red" }}>{auth.error.message}</p>}
      <button onClick={() => auth.signinRedirect()}>Sign In</button>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <Dashboard />
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}
