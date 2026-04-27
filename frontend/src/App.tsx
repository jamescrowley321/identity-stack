import { Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "react-oidc-context";
import { useEffect } from "react";
import { AppShell } from "./components/layout/AppShell";
import Dashboard from "./pages/Dashboard";
import RoleManagement from "./pages/RoleManagement";
import UserProfile from "./pages/UserProfile";
import TenantSettings from "./pages/TenantSettings";
import AccessKeys from "./pages/AccessKeys";
import MemberManagement from "./pages/MemberManagement";
import FGAManagement from "./pages/FGAManagement";
import { Button } from "./components/ui/button";
import { Alert, AlertTitle, AlertDescription } from "./components/ui/alert";

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

  if (auth.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-muted-foreground">Loading...</p>
      </div>
    );
  }

  if (auth.error) {
    return (
      <div className="flex min-h-screen items-center justify-center p-8">
        <Alert variant="destructive" className="max-w-md">
          <AlertTitle>Authentication Error</AlertTitle>
          <AlertDescription className="mt-2">
            <pre className="text-xs whitespace-pre-wrap">{auth.error.message}</pre>
          </AlertDescription>
          <Button variant="outline" size="sm" className="mt-3" onClick={() => auth.signinRedirect()}>
            Try Again
          </Button>
        </Alert>
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
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-muted-foreground">Loading...</p>
      </div>
    );
  }

  if (auth.isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4">
      <h1 className="text-3xl font-bold">Descope SaaS Starter</h1>
      {sessionExpired && (
        <p className="text-sm text-orange-500">Your session has expired. Please sign in again.</p>
      )}
      {auth.error && <p className="text-sm text-destructive">{auth.error.message}</p>}
      <Button onClick={() => auth.signinRedirect()}>Sign In</Button>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <ProtectedRoute>
            <AppShell />
          </ProtectedRoute>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="roles" element={<RoleManagement />} />
        <Route path="profile" element={<UserProfile />} />
        <Route path="settings" element={<TenantSettings />} />
        <Route path="keys" element={<AccessKeys />} />
        <Route path="members" element={<MemberManagement />} />
        <Route path="fga" element={<FGAManagement />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
