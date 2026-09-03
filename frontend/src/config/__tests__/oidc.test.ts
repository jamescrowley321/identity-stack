import { describe, it, expect } from "vitest";
import { buildOidcConfig, type OidcEnv } from "../oidc";

const ORIGIN = "https://app.example.com";

describe("buildOidcConfig", () => {
  describe("Descope backward-compat fallback (NFR-8)", () => {
    it("derives authority/client_id/scope from VITE_DESCOPE_* (byte-identical to legacy inline config)", () => {
      const env: OidcEnv = {
        VITE_DESCOPE_PROJECT_ID: "P123",
        VITE_DESCOPE_BASE_URL: "https://auth.custom.com",
      };

      const config = buildOidcConfig(env, ORIGIN);

      expect(config.authority).toBe("https://auth.custom.com/P123");
      expect(config.client_id).toBe("P123");
      expect(config.scope).toBe("openid profile email");
      expect(config.redirect_uri).toBe(ORIGIN);
    });

    it("defaults the authority base URL to https://api.descope.com when VITE_DESCOPE_BASE_URL is unset", () => {
      const env: OidcEnv = { VITE_DESCOPE_PROJECT_ID: "P123" };

      const config = buildOidcConfig(env, ORIGIN);

      expect(config.authority).toBe("https://api.descope.com/P123");
      expect(config.client_id).toBe("P123");
    });
  });

  describe("generic VITE_OIDC_* overrides (AC-1, AC-3)", () => {
    it("targets the configured OIDC provider when VITE_OIDC_* are set, overriding Descope vars", () => {
      const env: OidcEnv = {
        VITE_OIDC_AUTHORITY: "https://inspiring-nash-yli2uiwmcw.projects.oryapis.com",
        VITE_OIDC_CLIENT_ID: "58142046-5beb-420e-a4cd-310f7263357f",
        VITE_OIDC_SCOPE: "openid profile email offline_access",
        VITE_OIDC_REDIRECT_URI: "https://app.example.com/callback",
        // Descope vars still present — must be ignored in favour of the overrides.
        VITE_DESCOPE_PROJECT_ID: "P123",
        VITE_DESCOPE_BASE_URL: "https://auth.custom.com",
      };

      const config = buildOidcConfig(env, ORIGIN);

      expect(config.authority).toBe(
        "https://inspiring-nash-yli2uiwmcw.projects.oryapis.com",
      );
      expect(config.client_id).toBe("58142046-5beb-420e-a4cd-310f7263357f");
      expect(config.scope).toBe("openid profile email offline_access");
      expect(config.redirect_uri).toBe("https://app.example.com/callback");
    });

    it("allows OIDC vars to be mixed with Descope fallback per-field", () => {
      const env: OidcEnv = {
        VITE_OIDC_SCOPE: "openid profile email offline_access",
        VITE_DESCOPE_PROJECT_ID: "P123",
      };

      const config = buildOidcConfig(env, ORIGIN);

      // scope from OIDC override, authority/client_id from Descope fallback
      expect(config.scope).toBe("openid profile email offline_access");
      expect(config.authority).toBe("https://api.descope.com/P123");
      expect(config.client_id).toBe("P123");
    });
  });

  describe("empty / missing inputs (boundary)", () => {
    it("yields an empty client_id and a base-only authority when no provider env is set", () => {
      // Mirrors the legacy inline behavior: `${baseUrl}/${projectId}` with an
      // undefined projectId. Locked here so a fallback-chain change is caught.
      const config = buildOidcConfig({}, ORIGIN);

      expect(config.client_id).toBe("");
      expect(config.authority).toBe("https://api.descope.com/undefined");
      expect(config.scope).toBe("openid profile email");
      expect(config.redirect_uri).toBe(ORIGIN);
    });

    it("treats an empty VITE_DESCOPE_BASE_URL as unset and uses the default base (|| semantics)", () => {
      const env: OidcEnv = {
        VITE_DESCOPE_PROJECT_ID: "P123",
        VITE_DESCOPE_BASE_URL: "",
      };

      const config = buildOidcConfig(env, ORIGIN);

      expect(config.authority).toBe("https://api.descope.com/P123");
    });
  });

  describe("static config invariants", () => {
    it("uses the authorization-code flow with silent renew and a signin callback", () => {
      const config = buildOidcConfig({ VITE_DESCOPE_PROJECT_ID: "P123" }, ORIGIN);

      expect(config.response_type).toBe("code");
      expect(config.automaticSilentRenew).toBe(true);
      expect(typeof config.onSigninCallback).toBe("function");
    });

    it("defaults redirect_uri to window.location.origin when no origin argument is given", () => {
      const config = buildOidcConfig({ VITE_DESCOPE_PROJECT_ID: "P123" });

      expect(config.redirect_uri).toBe(window.location.origin);
    });
  });
});
