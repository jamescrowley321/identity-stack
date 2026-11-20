import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { useEffect } from "react";
import { IdentityProvider, useIdentityContext, type CanonicalIdentity } from "../IdentityContext";
import { useApiClient } from "../../hooks/useApiClient";
import { RequireRole } from "../../components/auth/RequireRole";
import { Unauthorized } from "../../components/Unauthorized";
import { AppShell } from "../../components/layout/AppShell";
import TenantSettings from "../../pages/TenantSettings";

// The real IdentityProvider and the real useApiClient, with only the OIDC
// session and the network stubbed: what a canonical-provider session (Zitadel,
// Ory) sends and shows.
const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

const twoTenants: CanonicalIdentity = {
  user: {
    id: "user-1",
    email: "alice@acme.example",
    user_name: "alice",
    given_name: "Alice",
    family_name: "Acme",
    status: "active",
  },
  roles: [
    { tenant_id: "tenant-acme", role_name: "admin", permissions: ["members.read"] },
    { tenant_id: "tenant-globex", role_name: "viewer", permissions: [] },
  ],
  tenant_memberships: [
    { tenant_id: "tenant-acme", tenant_name: "Acme", status: "active" },
    { tenant_id: "tenant-globex", tenant_name: "Globex", status: "active" },
  ],
  linked_idps: [{ provider_name: "zitadel", external_sub: "zitadel-sub-1" }],
};

interface SentRequest {
  url: string;
  tenant: string | null;
}

let sent: SentRequest[];

function stubNetwork(identity: Response | (() => Response)) {
  sent = [];
  globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const tenant = new Headers(init?.headers).get("X-Tenant-ID");
    sent.push({ url, tenant });
    if (url === "/api/identity") {
      return Promise.resolve(typeof identity === "function" ? identity() : identity);
    }
    if (url === "/api/tenants/current/settings") {
      // As the backend answers a canonical caller in several tenants: an
      // untenanted call is refused, a tenanted one answered in that tenant.
      return Promise.resolve(
        tenant === null
          ? json(403, { detail: "Tenant required" })()
          : json(200, { tenant_id: tenant, name: `Settings of ${tenant}`, custom_attributes: null })(),
      );
    }
    return Promise.resolve(new Response("{}", { status: 200 }));
  }) as typeof fetch;
}

/** The tenants named by every call to `url` since the network was last cleared. */
function tenantsSentTo(url: string): Set<string | null> {
  return new Set(sent.filter((r) => r.url === url).map((r) => r.tenant));
}

function json(status: number, body: unknown): () => Response {
  return () => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** A page that loads tenant data on mount and again whenever the client changes. */
function TenantPage() {
  const { apiFetch } = useApiClient();
  const { setCurrentTenantId } = useIdentityContext();
  const navigate = useNavigate();
  useEffect(() => {
    void apiFetch("/api/tenants/current");
  }, [apiFetch]);
  return (
    <div>
      <RequireRole role="admin" fallback={<Unauthorized />}>
        <p>Admin settings</p>
      </RequireRole>
      <button onClick={() => setCurrentTenantId("tenant-globex")}>pick globex</button>
      <button onClick={() => navigate("/settings")}>go to settings</button>
    </div>
  );
}

/** The app's own shell, with the routed page at `path`. */
function renderApp(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <IdentityProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<TenantPage />} />
            <Route path="settings" element={<TenantSettings />} />
          </Route>
        </Routes>
      </IdentityProvider>
    </MemoryRouter>,
  );
}

const STORAGE_KEY = "identity.currentTenantId";

describe("the active tenant on every API call", () => {
  beforeEach(() => {
    window.localStorage.clear();
    mockUseAuth.mockReturnValue({
      isAuthenticated: true,
      user: { access_token: "access-1", profile: { sub: "zitadel-sub-1" } },
      signinSilent: vi.fn(),
      signinRedirect: vi.fn(),
      removeUser: vi.fn(),
    });
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("sends the active tenant, and after a switch sends the new one and shows its roles", async () => {
    stubNetwork(json(200, twoTenants));
    renderApp();

    expect(await screen.findByText("Admin settings")).toBeInTheDocument();
    // The page mounts once the active tenant is known, so even its first call names it.
    await waitFor(() => expect(tenantsSentTo("/api/tenants/current")).toEqual(new Set(["tenant-acme"])));

    sent.length = 0;
    await act(async () => {
      screen.getByRole("button", { name: "pick globex" }).click();
    });

    // A viewer in Globex: the admin-only content gives way to the no-access state.
    expect(await screen.findByText("Access Denied")).toBeInTheDocument();
    expect(screen.queryByText("Admin settings")).not.toBeInTheDocument();
    // The page loads again in the new tenant, and nothing names the old one.
    await waitFor(() => expect(tenantsSentTo("/api/tenants/current")).toEqual(new Set(["tenant-globex"])));
    expect(sent.filter((r) => r.tenant === "tenant-acme")).toEqual([]);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("tenant-globex");
  });

  it("a page loaded straight away waits for the tenant: no untenanted call, no lasting refusal", async () => {
    stubNetwork(json(200, twoTenants));
    renderApp("/settings");

    expect(await screen.findByText("Settings of tenant-acme")).toBeInTheDocument();
    expect(screen.queryByText("Access Denied")).not.toBeInTheDocument();
    expect(tenantsSentTo("/api/tenants/current/settings")).toEqual(new Set(["tenant-acme"]));
  });

  it("moving to another page keeps the identity: no reload of it and no loading screen", async () => {
    stubNetwork(json(200, twoTenants));
    renderApp();
    expect(await screen.findByText("Admin settings")).toBeInTheDocument();

    await act(async () => {
      screen.getByRole("button", { name: "go to settings" }).click();
    });

    // The next page mounts straight away in the active tenant; the shell never
    // drops back to its loading state, which an identity reload would cause.
    expect(screen.queryByText("Loading...")).not.toBeInTheDocument();
    expect(await screen.findByText("Settings of tenant-acme")).toBeInTheDocument();
    expect(sent.filter((r) => r.url === "/api/identity")).toEqual([{ url: "/api/identity", tenant: null }]);
  });

  it("never sends a tenant with the identity request, which spans every tenant", async () => {
    window.localStorage.setItem(STORAGE_KEY, "tenant-globex");
    stubNetwork(json(200, twoTenants));
    renderApp();

    expect(await screen.findByText("Access Denied")).toBeInTheDocument();
    expect(sent.filter((r) => r.url === "/api/identity")).toEqual([{ url: "/api/identity", tenant: null }]);
  });

  it.each([
    ["the organization is not linked", 403, { detail: "Organization is not linked to a tenant" }],
    ["the email is held by another user", 409, { detail: "Could not provision identity" }],
    ["no verified email", 400, { detail: "A verified email is required to provision an identity" }],
  ])("a refused first login (%s) shows the no-access state and sends no tenant", async (_cause, status, body) => {
    stubNetwork(json(status, body));
    renderApp();

    expect(await screen.findByText("Access Denied")).toBeInTheDocument();
    expect(screen.getByText("No tenants")).toBeInTheDocument();
    expect(screen.queryByText("Admin settings")).not.toBeInTheDocument();
    expect(sent.filter((r) => r.tenant !== null)).toEqual([]);
  });
});
