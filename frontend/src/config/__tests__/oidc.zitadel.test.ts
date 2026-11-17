import { describe, it, expect } from "vitest";
import { buildOidcConfig, readTenantScopeTemplate, type OidcEnv } from "../oidc";

// What `make zitadel-frontend` passes to the build: the `frontend_environment`
// output of infra/zitadel, plus an empty tenant scope template.
const zitadelEnv: OidcEnv = {
  VITE_OIDC_AUTHORITY: "http://zitadel.localhost:8480",
  VITE_OIDC_CLIENT_ID: "349876543210987654",
  VITE_OIDC_REDIRECT_URI: "http://localhost:3000",
  VITE_OIDC_SCOPE: "openid profile email offline_access urn:zitadel:iam:user:resourceowner",
  VITE_OIDC_TENANT_SCOPE_TEMPLATE: "",
};

describe("the SPA built against Zitadel", () => {
  it("is a code-flow client of the Zitadel issuer with the organization scope, from env alone", () => {
    const { onSigninCallback, ...config } = buildOidcConfig(zitadelEnv, "http://ignored.example");

    expect(config).toEqual({
      authority: "http://zitadel.localhost:8480",
      client_id: "349876543210987654",
      redirect_uri: "http://localhost:3000",
      scope: "openid profile email offline_access urn:zitadel:iam:user:resourceowner",
      response_type: "code",
      automaticSilentRenew: true,
    });
    expect(typeof onSigninCallback).toBe("function");
  });

  it("switches tenant on the client: no tenant scope template", () => {
    expect(readTenantScopeTemplate(zitadelEnv)).toBeNull();
  });
});
