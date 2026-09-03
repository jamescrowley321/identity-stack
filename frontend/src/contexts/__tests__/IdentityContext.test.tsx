import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import type { ReactNode } from "react";
import {
  IdentityProvider,
  useIdentityContext,
  type CanonicalIdentity,
} from "../IdentityContext";

// IdentityProvider fetches `GET /api/identity` once per authenticated session
// via useApiClient().apiFetch, guarded on useAuth().isAuthenticated. We mock
// both so the provider's fetch/guard/fail-closed logic is exercised directly.
const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockApiFetch = vi.fn();
vi.mock("../../hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: mockApiFetch }),
}));

const oryIdentity: CanonicalIdentity = {
  user: {
    id: "ory-user-1",
    email: "alice@ory.example",
    user_name: "alice",
    given_name: "Alice",
    family_name: "Ory",
    status: "active",
  },
  roles: [{ tenant_id: "t1", role_name: "admin", permissions: ["docs.read"] }],
  tenant_memberships: [
    { tenant_id: "t1", tenant_name: "Acme" },
    { tenant_id: "t2", tenant_name: "Beta" },
  ],
  linked_idps: [{ provider_name: "ory", external_sub: "ory|abc" }],
};

const descopeIdentity: CanonicalIdentity = {
  ...oryIdentity,
  user: { ...oryIdentity.user, id: "descope-user-1", email: "bob@descope.example" },
  linked_idps: [{ provider_name: "descope", external_sub: "descope|xyz" }],
};

/** Reads the context and renders each field for assertion. */
function Consumer() {
  const { identity, loading, error, currentTenantId, setCurrentTenantId } =
    useIdentityContext();
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="error">{error ? error.message : "none"}</span>
      <span data-testid="current-tenant">{currentTenantId ?? "null"}</span>
      <span data-testid="email">{identity?.user.email ?? "no-identity"}</span>
      <button onClick={() => setCurrentTenantId("t2")}>switch</button>
    </div>
  );
}

function renderWithProvider(children: ReactNode = <Consumer />) {
  return render(<IdentityProvider>{children}</IdentityProvider>);
}

function okResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

const STORAGE_KEY = "identity.currentTenantId";

describe("IdentityProvider / useIdentityContext", () => {
  beforeEach(() => {
    mockUseAuth.mockReset();
    mockApiFetch.mockReset();
    window.localStorage.clear();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("does not fetch identity when unauthenticated (avoids /login bounce)", async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: false });
    renderWithProvider();

    // Give any (erroneous) effect a chance to fire.
    await Promise.resolve();
    expect(mockApiFetch).not.toHaveBeenCalled();
    expect(screen.getByTestId("email").textContent).toBe("no-identity");
    expect(screen.getByTestId("current-tenant").textContent).toBe("null");
  });

  it("fetches and exposes the canonical identity when authenticated", async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true });
    mockApiFetch.mockResolvedValue(okResponse(oryIdentity));
    renderWithProvider();

    await waitFor(() =>
      expect(screen.getByTestId("email").textContent).toBe("alice@ory.example"),
    );
    expect(mockApiFetch).toHaveBeenCalledWith("/api/identity");
    expect(screen.getByTestId("current-tenant").textContent).toBe("t1"); // first membership
    expect(screen.getByTestId("error").textContent).toBe("none");
  });

  it("exposes an Ory and a Descope payload identically (provider-neutral)", async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true });

    mockApiFetch.mockResolvedValue(okResponse(oryIdentity));
    const ory = renderWithProvider();
    await waitFor(() =>
      expect(ory.getByTestId("email").textContent).toBe("alice@ory.example"),
    );
    expect(ory.getByTestId("current-tenant").textContent).toBe("t1");
    ory.unmount();

    mockApiFetch.mockResolvedValue(okResponse(descopeIdentity));
    const descope = renderWithProvider();
    await waitFor(() =>
      expect(descope.getByTestId("email").textContent).toBe("bob@descope.example"),
    );
    // Same tenant memberships -> same default current tenant regardless of IdP.
    expect(descope.getByTestId("current-tenant").textContent).toBe("t1");
  });

  it("fails closed to null identity on a non-ok response", async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true });
    mockApiFetch.mockResolvedValue(new Response("nope", { status: 500 }));
    renderWithProvider();

    await waitFor(() =>
      expect(screen.getByTestId("error").textContent).toContain("identity fetch failed: 500"),
    );
    expect(screen.getByTestId("email").textContent).toBe("no-identity");
    expect(screen.getByTestId("current-tenant").textContent).toBe("null");
  });

  it("fails closed to null identity when apiFetch rejects", async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true });
    mockApiFetch.mockRejectedValue(new Error("network down"));
    renderWithProvider();

    await waitFor(() =>
      expect(screen.getByTestId("error").textContent).toBe("network down"),
    );
    expect(screen.getByTestId("email").textContent).toBe("no-identity");
  });

  it("honors a valid localStorage tenant selection as currentTenantId", async () => {
    window.localStorage.setItem(STORAGE_KEY, "t2");
    mockUseAuth.mockReturnValue({ isAuthenticated: true });
    mockApiFetch.mockResolvedValue(okResponse(oryIdentity));
    renderWithProvider();

    await waitFor(() =>
      expect(screen.getByTestId("email").textContent).toBe("alice@ory.example"),
    );
    expect(screen.getByTestId("current-tenant").textContent).toBe("t2");
  });

  it("falls back to the first membership when the stored selection is stale", async () => {
    window.localStorage.setItem(STORAGE_KEY, "no-longer-a-member");
    mockUseAuth.mockReturnValue({ isAuthenticated: true });
    mockApiFetch.mockResolvedValue(okResponse(oryIdentity));
    renderWithProvider();

    await waitFor(() =>
      expect(screen.getByTestId("email").textContent).toBe("alice@ory.example"),
    );
    expect(screen.getByTestId("current-tenant").textContent).toBe("t1");
  });

  it("setCurrentTenantId updates the active tenant and persists it", async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true });
    mockApiFetch.mockResolvedValue(okResponse(oryIdentity));
    renderWithProvider();

    await waitFor(() =>
      expect(screen.getByTestId("current-tenant").textContent).toBe("t1"),
    );

    act(() => {
      screen.getByText("switch").click();
    });

    expect(screen.getByTestId("current-tenant").textContent).toBe("t2");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("t2");
  });

  it("throws when useIdentityContext is used outside an IdentityProvider", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Consumer />)).toThrow(
      /useIdentityContext must be used within an IdentityProvider/,
    );
    spy.mockRestore();
  });
});
