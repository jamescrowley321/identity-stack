import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { IdentityProvider, useIdentityContext, type CanonicalIdentity } from "../IdentityContext";
import { TENANT_SELECTION_KEY } from "../tenantSelection";
import { buildOidcConfig } from "../../config/oidc";

// The tenant the selector starts on: the membership whose organization the
// sign-in named (Keycloak `organization`, Zitadel
// `urn:zitadel:iam:user:resourceowner:id`), else the first membership.
const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));
vi.mock("../../hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: (url: string) => fetch(url) }),
}));

const ZITADEL_CLAIM = "urn:zitadel:iam:user:resourceowner:id";

function identity(memberships: CanonicalIdentity["tenant_memberships"]): CanonicalIdentity {
  return {
    user: {
      id: "user-bob",
      email: "bob@globex.test",
      user_name: "bob",
      given_name: "Bob",
      family_name: "Two-Orgs",
      status: "active",
    },
    roles: memberships.map((m) => ({ tenant_id: m.tenant_id, role_name: "member", permissions: [] })),
    tenant_memberships: memberships,
    linked_idps: [],
  };
}

const keycloakBob = identity([
  { tenant_id: "tenant-acme", tenant_name: "Acme", external_org_id: "acme" },
  { tenant_id: "tenant-globex", tenant_name: "Globex", external_org_id: "globex" },
]);

const zitadelBob = identity([
  { tenant_id: "tenant-acme", tenant_name: "Acme", external_org_id: "300000000000000001" },
  { tenant_id: "tenant-globex", tenant_name: "Globex", external_org_id: "300000000000000002" },
]);

function base64url(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function jwt(claims: Record<string, unknown>): string {
  return `${base64url({ alg: "RS256", typ: "JWT" })}.${base64url(claims)}.signature`;
}

function signedIn(profile: Record<string, unknown>, accessToken = "opaque-access-token") {
  mockUseAuth.mockReturnValue({
    isAuthenticated: true,
    user: { access_token: accessToken, profile: { sub: "sub-bob", ...profile } },
  });
}

function serve(body: CanonicalIdentity) {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }),
  ) as typeof fetch;
}

function ActiveTenant() {
  const { currentTenantId } = useIdentityContext();
  return <p>active: {currentTenantId ?? "none"}</p>;
}

async function activeTenant(body: CanonicalIdentity): Promise<string> {
  serve(body);
  render(
    <IdentityProvider>
      <ActiveTenant />
    </IdentityProvider>,
  );
  await waitFor(() => expect(screen.getByText(/^active:/)).not.toHaveTextContent("active: none"));
  return (screen.getByText(/^active:/).textContent ?? "").replace("active: ", "");
}

describe("the tenant a sign-in opens", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => window.localStorage.clear());

  it("is the Keycloak organization the token's alias list names", async () => {
    signedIn({ organization: ["globex"] });
    expect(await activeTenant(keycloakBob)).toBe("tenant-globex");
  });

  it("is the Keycloak organization the token's alias map names", async () => {
    signedIn({ organization: { globex: { id: "5f0c7e2a-0000-4000-8000-000000000002" } } });
    expect(await activeTenant(keycloakBob)).toBe("tenant-globex");
  });

  it("is the Keycloak organization the access token names when the ID token names none", async () => {
    signedIn({}, jwt({ sub: "sub-bob", organization: ["globex"] }));
    expect(await activeTenant(keycloakBob)).toBe("tenant-globex");
  });

  it("is the Zitadel organization the access token's resourceowner claim names", async () => {
    signedIn({}, jwt({ sub: "sub-bob", [ZITADEL_CLAIM]: "300000000000000002" }));
    expect(await activeTenant(zitadelBob)).toBe("tenant-globex");
  });

  it("is the Zitadel organization the ID token's resourceowner claim names", async () => {
    signedIn({ [ZITADEL_CLAIM]: "300000000000000002" });
    expect(await activeTenant(zitadelBob)).toBe("tenant-globex");
  });

  it("is the first membership when the token names no organization", async () => {
    signedIn({});
    expect(await activeTenant(keycloakBob)).toBe("tenant-acme");
  });

  it("is the first membership when the token names two Keycloak organizations", async () => {
    signedIn({ organization: ["globex", "acme"] });
    expect(await activeTenant(keycloakBob)).toBe("tenant-acme");
  });

  it("is the first membership when no membership is the named organization's", async () => {
    signedIn({ organization: ["initech"] });
    expect(await activeTenant(keycloakBob)).toBe("tenant-acme");
  });

  it("is the first membership when the named organization's tenant is linked at another provider", async () => {
    // The backend gives null for a tenant linked to no organization, or to another provider's.
    signedIn({ organization: ["globex"] });
    expect(
      await activeTenant(
        identity([
          { tenant_id: "tenant-acme", tenant_name: "Acme", external_org_id: null },
          { tenant_id: "tenant-globex", tenant_name: "Globex", external_org_id: null },
        ]),
      ),
    ).toBe("tenant-acme");
  });

  it("is the first membership when the payload carries no external_org_id", async () => {
    signedIn({ organization: ["globex"] });
    expect(
      await activeTenant(
        identity([
          { tenant_id: "tenant-acme", tenant_name: "Acme" },
          { tenant_id: "tenant-globex", tenant_name: "Globex" },
        ]),
      ),
    ).toBe("tenant-acme");
  });

  it("gives way to a tenant chosen in this sign-in's session (a reload)", async () => {
    window.localStorage.setItem(TENANT_SELECTION_KEY, "tenant-acme");
    signedIn({ organization: ["globex"] });
    expect(await activeTenant(keycloakBob)).toBe("tenant-acme");
  });

  it("replaces a tenant chosen before the sign-in: the sign-in callback forgets it", async () => {
    window.localStorage.setItem(TENANT_SELECTION_KEY, "tenant-acme");
    buildOidcConfig({ VITE_OIDC_AUTHORITY: "http://idp.example", VITE_OIDC_CLIENT_ID: "spa" }).onSigninCallback();
    expect(window.localStorage.getItem(TENANT_SELECTION_KEY)).toBeNull();

    signedIn({ organization: ["globex"] });
    expect(await activeTenant(keycloakBob)).toBe("tenant-globex");
  });
});
