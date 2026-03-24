import { Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "react-oidc-context";
import { useEffect } from "react";
import Dashboard from "./pages/Dashboard";
import RoleManagement from "./pages/RoleManagement";
import UserProfile from "./pages/UserProfile";
import TenantSettings from "./pages/TenantSettings";

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const auth = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    const handleExpired = () => {
      auth.removeUser();
      navigate("/login", { state: { sessionExpired: true } });
    };

    auth.events.addAccessTokenExpired(handleExpired);
    auth.events.addSilentRenewError(handleExpired);
    return () => {
      auth.events.removeAccessTokenExpired(handleExpired);
      auth.events.removeSilentRenewError(handleExpired);
    };
  }, [auth, navigate]);

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
  const location = useLocation();
  const sessionExpired = (location.state as { sessionExpired?: boolean })?.sessionExpired;

  if (auth.isLoading) {
    return <div style={{ padding: "2rem" }}>Loading...</div>;
  }

  if (auth.isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  return (
    <div style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "100vh", flexDirection: "column", gap: "1rem" }}>
      <h1>Descope SaaS Starter</h1>
      {sessionExpired && <p style={{ color: "orange" }}>Your session has expired. Please sign in again.</p>}
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
      <Route
        path="/roles"
        element={
          <ProtectedRoute>
            <RoleManagement />
          </ProtectedRoute>
        }
      />
      <Route
        path="/profile"
        element={
          <ProtectedRoute>
            <UserProfile />
          </ProtectedRoute>
        }
      />
      <Route
        path="/settings"
        element={
          <ProtectedRoute>
            <TenantSettings />
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}
