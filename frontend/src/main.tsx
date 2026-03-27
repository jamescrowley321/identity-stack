import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "react-oidc-context";
import App from "./App";
import "./index.css";

const projectId = import.meta.env.VITE_DESCOPE_PROJECT_ID;
const baseUrl = import.meta.env.VITE_DESCOPE_BASE_URL || "https://api.descope.com";

const oidcConfig = {
  authority: `${baseUrl}/${projectId}`,
  client_id: projectId,
  redirect_uri: window.location.origin,
  post_logout_redirect_uri: window.location.origin,
  scope: "openid profile email",
  response_type: "code",
  automaticSilentRenew: true,
  onSigninCallback: () => {
    // Remove code/state from URL after successful code exchange
    window.history.replaceState({}, document.title, window.location.pathname);
  },
};

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider {...oidcConfig}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </AuthProvider>
  </StrictMode>,
);
