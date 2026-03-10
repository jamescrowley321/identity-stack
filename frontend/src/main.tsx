import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "react-oidc-context";
import type { WebStorageStateStore } from "oidc-client-ts";
import App from "./App";

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
