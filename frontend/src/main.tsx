import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "react-oidc-context";
import { ThemeProvider } from "next-themes";
import { TooltipProvider } from "@/components/ui/tooltip";
import { IdentityProvider } from "./contexts/IdentityContext";
import App from "./App";
import { buildOidcConfig } from "@/config/oidc";
import "./index.css";

const oidcConfig = buildOidcConfig(import.meta.env);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
      <TooltipProvider>
        <AuthProvider {...oidcConfig}>
          <BrowserRouter>
            <IdentityProvider>
              <App />
            </IdentityProvider>
          </BrowserRouter>
        </AuthProvider>
      </TooltipProvider>
    </ThemeProvider>
  </StrictMode>,
);
