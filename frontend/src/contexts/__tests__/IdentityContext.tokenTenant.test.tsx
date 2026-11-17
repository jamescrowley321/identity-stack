import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import {
  IdentityProvider,
  useIdentityContext,
  type CanonicalIdentity,
} from "../IdentityContext";

// A provider whose tenant comes from the token: `GET /api/identity` carries
// `token_tenant`, `roles` is empty and the memberships are the provider's.
const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockApiFetch = vi.fn();
vi.mock("../../hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: mockApiFetch }),
}));

const TEMPLATE = "openid profile email organization:{tenant}";

const tokenTenantIdentity: CanonicalIdentity = {
  user: {
    id: "user-1",
    email: "carol@example.test",
    user_name: "carol",
    given_name: "Carol",
    family_name: "Example",
    status: "active",
  },
  roles: [],
  tenant_memberships: [
    { tenant_id: "acme", tenant_name: "Acme" },
    { tenant_id: "globex", tenant_name: "Globex" },
  ],
  linked_idps: [{ provider_name: "keycloak", external_sub: "kc-carol" }],
  token_tenant: { tenant_id: "globex", permissions: ["documents.read"] },
};

const clientSideIdentity: CanonicalIdentity = {
  ...tokenTenantIdentity,
  roles: [{ tenant_id: "acme", role_name: "admin", permissions: ["documents.read"] }],
  token_tenant: undefined,
};

function okResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function authWith(overrides: Record<string, unknown> = {}) {
  return {
    isAuthenticated: true,
    user: { profile: { sub: "kc-carol" }, state: undefined },
    signinRedirect: vi.fn(() => Promise.resolve()),
    ...overrides,
  };
}

function Consumer() {
  const ctx = useIdentityContext();
  return (
    <div>
      <span data-testid="loading">{String(ctx.loading)}</span>
      <span data-testid="current-tenant">{ctx.currentTenantId ?? "null"}</span>
      <span data-testid="tenant-switch">{ctx.tenantSwitch}</span>
      <span data-testid="switch-failure">
        {ctx.tenantSwitchFailure ? ctx.tenantSwitchFailure.requestedTenantId : "none"}
      </span>
      <button onClick={() => ctx.setCurrentTenantId("acme")}>select acme</button>
      <button onClick={() => void ctx.signInToTenant("acme")}>sign in to acme</button>
      <button onClick={() => void ctx.signInAgain()}>sign in again</button>
    </div>
  );
}

async function loaded() {
  await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
}

describe("IdentityProvider with a tenant that comes from the token", () => {
  beforeEach(() => {
    mockUseAuth.mockReset();
    mockApiFetch.mockReset();
    window.localStorage.clear();
  });

  it("makes the token's tenant current, whatever the stored selection says", async () => {
    window.localStorage.setItem("identity.currentTenantId", "acme");
    mockUseAuth.mockReturnValue(authWith());
    mockApiFetch.mockResolvedValue(okResponse(tokenTenantIdentity));

    render(
      <IdentityProvider tenantScopeTemplate={TEMPLATE}>
        <Consumer />
      </IdentityProvider>,
    );
    await loaded();

    expect(screen.getByTestId("current-tenant")).toHaveTextContent("globex");
    act(() => screen.getByText("select acme").click());
    expect(screen.getByTestId("current-tenant")).toHaveTextContent("globex");
  });

  it("switches by signing in when the scope template is set", async () => {
    const auth = authWith();
    mockUseAuth.mockReturnValue(auth);
    mockApiFetch.mockResolvedValue(okResponse(tokenTenantIdentity));

    render(
      <IdentityProvider tenantScopeTemplate={TEMPLATE}>
        <Consumer />
      </IdentityProvider>,
    );
    await loaded();
    expect(screen.getByTestId("tenant-switch")).toHaveTextContent("sign-in");

    act(() => screen.getByText("sign in to acme").click());

    expect(auth.signinRedirect).toHaveBeenCalledWith({
      scope: "openid profile email organization:acme",
      state: { requestedTenantId: "acme" },
    });
  });

  it("offers no switch when the scope template is unset", async () => {
    const auth = authWith();
    mockUseAuth.mockReturnValue(auth);
    mockApiFetch.mockResolvedValue(okResponse(tokenTenantIdentity));

    render(
      <IdentityProvider tenantScopeTemplate={null}>
        <Consumer />
      </IdentityProvider>,
    );
    await loaded();

    expect(screen.getByTestId("tenant-switch")).toHaveTextContent("none");
    act(() => screen.getByText("sign in to acme").click());
    expect(auth.signinRedirect).not.toHaveBeenCalled();
  });

  it("keeps the switch client-side for a tenant that is not in the token, template or not", async () => {
    const auth = authWith();
    mockUseAuth.mockReturnValue(auth);
    mockApiFetch.mockResolvedValue(okResponse(clientSideIdentity));

    render(
      <IdentityProvider tenantScopeTemplate={TEMPLATE}>
        <Consumer />
      </IdentityProvider>,
    );
    await loaded();

    expect(screen.getByTestId("tenant-switch")).toHaveTextContent("client");
    act(() => screen.getByText("sign in to acme").click());
    expect(auth.signinRedirect).not.toHaveBeenCalled();
  });

  it("reports no failure when the returned token names the tenant asked for", async () => {
    mockUseAuth.mockReturnValue(
      authWith({ user: { profile: { sub: "kc-carol" }, state: { requestedTenantId: "globex" } } }),
    );
    mockApiFetch.mockResolvedValue(okResponse(tokenTenantIdentity));

    render(
      <IdentityProvider tenantScopeTemplate={TEMPLATE}>
        <Consumer />
      </IdentityProvider>,
    );
    await loaded();

    expect(screen.getByTestId("switch-failure")).toHaveTextContent("none");
    expect(screen.getByTestId("current-tenant")).toHaveTextContent("globex");
  });

  it("reports a failure, and keeps the token's tenant current, when the token names another tenant", async () => {
    mockUseAuth.mockReturnValue(
      authWith({ user: { profile: { sub: "kc-carol" }, state: { requestedTenantId: "acme" } } }),
    );
    mockApiFetch.mockResolvedValue(okResponse(tokenTenantIdentity));

    render(
      <IdentityProvider tenantScopeTemplate={TEMPLATE}>
        <Consumer />
      </IdentityProvider>,
    );
    await loaded();

    expect(screen.getByTestId("switch-failure")).toHaveTextContent("acme");
    expect(screen.getByTestId("current-tenant")).toHaveTextContent("globex");
  });

  it("reports a failure when the token names no tenant and identity is refused", async () => {
    mockUseAuth.mockReturnValue(
      authWith({ user: { profile: { sub: "kc-carol" }, state: { requestedTenantId: "acme" } } }),
    );
    mockApiFetch.mockResolvedValue(new Response("{}", { status: 403 }));

    render(
      <IdentityProvider tenantScopeTemplate={TEMPLATE}>
        <Consumer />
      </IdentityProvider>,
    );
    await loaded();

    expect(screen.getByTestId("switch-failure")).toHaveTextContent("acme");
    expect(screen.getByTestId("current-tenant")).toHaveTextContent("null");
  });

  it("signs in again with the configured scope, asking for no tenant", async () => {
    const auth = authWith();
    mockUseAuth.mockReturnValue(auth);
    mockApiFetch.mockResolvedValue(okResponse(tokenTenantIdentity));

    render(
      <IdentityProvider tenantScopeTemplate={TEMPLATE}>
        <Consumer />
      </IdentityProvider>,
    );
    await loaded();
    act(() => screen.getByText("sign in again").click());

    expect(auth.signinRedirect).toHaveBeenCalledWith();
  });
});
