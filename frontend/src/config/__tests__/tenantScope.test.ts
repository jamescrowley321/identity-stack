import { describe, it, expect } from "vitest";
import {
  readTenantScopeTemplate,
  tenantScope,
  TenantIdNotAScopeToken,
  TenantScopeTemplateInvalid,
} from "../oidc";

const KEYCLOAK_TEMPLATE = "openid profile email organization:{tenant}";

describe("readTenantScopeTemplate", () => {
  it("is null when VITE_OIDC_TENANT_SCOPE_TEMPLATE is unset", () => {
    expect(readTenantScopeTemplate({})).toBeNull();
  });

  it("is null when VITE_OIDC_TENANT_SCOPE_TEMPLATE is an empty line", () => {
    expect(readTenantScopeTemplate({ VITE_OIDC_TENANT_SCOPE_TEMPLATE: "" })).toBeNull();
  });

  it("returns the template when it names the tenant once", () => {
    expect(
      readTenantScopeTemplate({ VITE_OIDC_TENANT_SCOPE_TEMPLATE: KEYCLOAK_TEMPLATE }),
    ).toBe(KEYCLOAK_TEMPLATE);
  });

  it("refuses a template without the {tenant} placeholder", () => {
    expect(() =>
      readTenantScopeTemplate({ VITE_OIDC_TENANT_SCOPE_TEMPLATE: "openid organization" }),
    ).toThrow(TenantScopeTemplateInvalid);
  });

  it("refuses a template naming the tenant twice", () => {
    expect(() =>
      readTenantScopeTemplate({
        VITE_OIDC_TENANT_SCOPE_TEMPLATE: "openid a:{tenant} b:{tenant}",
      }),
    ).toThrow(TenantScopeTemplateInvalid);
  });
});

describe("tenantScope", () => {
  it("puts the tenant id where the template says", () => {
    expect(tenantScope(KEYCLOAK_TEMPLATE, "acme")).toBe(
      "openid profile email organization:acme",
    );
  });

  it.each([
    ["contains a space", "acme offline_access"],
    ["is empty", ""],
    ["contains a double quote", 'ac"me'],
    ["contains a backslash", "ac\\me"],
    ["contains a tab", "ac\tme"],
  ])("refuses a tenant id that %s, so it can add no scope", (_why, tenantId) => {
    expect(() => tenantScope(KEYCLOAK_TEMPLATE, tenantId)).toThrow(TenantIdNotAScopeToken);
  });
});
