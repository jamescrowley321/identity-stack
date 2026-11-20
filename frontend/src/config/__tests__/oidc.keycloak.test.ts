import { describe, it, expect } from "vitest";
import { buildOidcConfig, type OidcEnv } from "../oidc";

// The `frontend_environment` output of infra/keycloak, which `make keycloak-env`
// prints and the Keycloak e2e job builds the SPA with.
const keycloakEnv: OidcEnv = {
  VITE_OIDC_AUTHORITY: "http://keycloak.localhost:8180/realms/identity-stack",
  VITE_OIDC_CLIENT_ID: "identity-stack-spa",
  VITE_OIDC_REDIRECT_URI: "http://localhost:3000",
  VITE_OIDC_SCOPE: "openid profile email organization",
};

describe("the SPA built against Keycloak", () => {
  it("is a code-flow client of the realm with the plain organization scope, from env alone", () => {
    const { onSigninCallback, ...config } = buildOidcConfig(keycloakEnv, "http://ignored.example");

    expect(config).toEqual({
      authority: "http://keycloak.localhost:8180/realms/identity-stack",
      client_id: "identity-stack-spa",
      redirect_uri: "http://localhost:3000",
      scope: "openid profile email organization",
      response_type: "code",
      automaticSilentRenew: true,
    });
    expect(typeof onSigninCallback).toBe("function");
  });
});
