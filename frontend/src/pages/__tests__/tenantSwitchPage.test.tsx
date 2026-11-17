import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ComponentType } from "react";
import { IdentityProvider, useIdentityContext, type CanonicalIdentity } from "@/contexts/IdentityContext";
import { AppShell } from "@/components/layout/AppShell";
import MemberManagement from "../MemberManagement";
import AccessKeys from "../AccessKeys";

// The real shell, IdentityProvider, useApiClient and pages; only the OIDC
// session and the network are stubbed. A page that was busy in one tenant must
// show and act on nothing of it once the user has switched to another.
const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
  Toaster: () => null,
}));

const identity: CanonicalIdentity = {
  user: {
    id: "user-1",
    email: "alice@acme.example",
    user_name: "alice",
    given_name: "Alice",
    family_name: "Acme",
    status: "active",
  },
  roles: [
    { tenant_id: "tenant-acme", role_name: "admin", permissions: [] },
    { tenant_id: "tenant-globex", role_name: "admin", permissions: [] },
  ],
  tenant_memberships: [
    { tenant_id: "tenant-acme", tenant_name: "Acme" },
    { tenant_id: "tenant-globex", tenant_name: "Globex" },
  ],
  linked_idps: [{ provider_name: "zitadel", external_sub: "zitadel-sub-1" }],
};

interface SentRequest {
  method: string;
  url: string;
  tenant: string | null;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** A response the test releases when it chooses. */
function deferred() {
  let release!: (response: Response) => void;
  const promise = new Promise<Response>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

interface ListPage {
  name: string;
  Page: ComponentType;
  list: string;
  rowAction: string;
  body: (row: string) => unknown;
}

const pages: ListPage[] = [
  {
    name: "MemberManagement",
    Page: MemberManagement,
    list: "/api/members",
    rowAction: "Remove",
    body: (row) => ({ members: [{ userId: `${row}-id`, name: row, status: "enabled", roleNames: [] }] }),
  },
  {
    name: "AccessKeys",
    Page: AccessKeys,
    list: "/api/keys",
    rowAction: "Delete",
    body: (row) => ({ keys: [{ id: `${row}-id`, name: row, status: "active" }] }),
  },
];

let sent: SentRequest[];
/** Requests the test answers itself, keyed by "METHOD url tenant". */
let held: Map<string, ReturnType<typeof deferred>>;

function hold(method: string, url: string, tenant: string) {
  const answer = deferred();
  held.set(`${method} ${url} ${tenant}`, answer);
  return answer;
}

/** Each tenant's list holds one row named after the tenant; every write succeeds. */
function stubNetwork(page: ListPage, extra: (r: SentRequest) => Response | undefined = () => undefined) {
  sent = [];
  held = new Map();
  globalThis.fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const request: SentRequest = {
      method: init?.method ?? "GET",
      url: String(input),
      tenant: new Headers(init?.headers).get("X-Tenant-ID"),
    };
    sent.push(request);
    const answer = held.get(`${request.method} ${request.url} ${request.tenant}`);
    if (answer) {
      held.delete(`${request.method} ${request.url} ${request.tenant}`);
      return answer.promise;
    }
    if (request.url === "/api/identity") return Promise.resolve(json(200, identity));
    if (request.method === "GET" && request.url === page.list) {
      return Promise.resolve(json(200, page.body(`${request.tenant}-row`)));
    }
    return Promise.resolve(extra(request) ?? json(200, {}));
  }) as typeof fetch;
}

function SwitchToGlobex() {
  const { setCurrentTenantId } = useIdentityContext();
  return <button onClick={() => setCurrentTenantId("tenant-globex")}>switch to globex</button>;
}

function renderPage(Page: ComponentType) {
  return render(
    <MemoryRouter>
      <IdentityProvider>
        <SwitchToGlobex />
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<Page />} />
          </Route>
        </Routes>
      </IdentityProvider>
    </MemoryRouter>,
  );
}

async function switchToGlobex() {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "switch to globex" }));
  });
}

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
  vi.useRealTimers();
  window.localStorage.clear();
});

describe.each(pages)("$name across a tenant switch", (page) => {
  it("a row action started in the previous tenant does not refill the list with its rows", async () => {
    stubNetwork(page);
    renderPage(page.Page);
    expect(await screen.findByText("tenant-acme-row")).toBeInTheDocument();

    const action = hold("DELETE", `${page.list}/tenant-acme-row-id`, "tenant-acme");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: page.rowAction }));
    });
    await switchToGlobex();
    expect(await screen.findByText("tenant-globex-row")).toBeInTheDocument();

    await act(async () => {
      action.release(json(200, {}));
    });

    await waitFor(() => expect(screen.getByText("tenant-globex-row")).toBeInTheDocument());
    expect(screen.queryByText("tenant-acme-row")).not.toBeInTheDocument();
  });

  it("shows no row of the previous tenant, and so offers no action on one, while the new tenant loads", async () => {
    stubNetwork(page);
    renderPage(page.Page);
    expect(await screen.findByText("tenant-acme-row")).toBeInTheDocument();

    const globexList = hold("GET", page.list, "tenant-globex");
    await switchToGlobex();

    expect(screen.queryByText("tenant-acme-row")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: page.rowAction })).not.toBeInTheDocument();

    await act(async () => {
      globexList.release(json(200, page.body("tenant-globex-row")));
    });
    expect(await screen.findByText("tenant-globex-row")).toBeInTheDocument();
    expect(sent.filter((r) => r.method !== "GET")).toEqual([]);
  });
});

describe("AccessKeys across a tenant switch", () => {
  const keysPage = pages.find((p) => p.name === "AccessKeys")!;
  const created = (r: SentRequest) =>
    r.method === "POST" && r.url === "/api/keys"
      ? json(200, { cleartext: `secret-of-${r.tenant}` })
      : undefined;

  async function createKey(name: string) {
    fireEvent.change(screen.getByPlaceholderText("Key name"), { target: { value: name } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Create" }));
    });
  }

  it("drops the previous tenant's new-key secret and the half-typed name", async () => {
    stubNetwork(keysPage, created);
    renderPage(AccessKeys);
    expect(await screen.findByText("tenant-acme-row")).toBeInTheDocument();

    await createKey("ci key");
    expect(await screen.findByText("secret-of-tenant-acme")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Key name"), { target: { value: "half typed" } });

    await switchToGlobex();
    expect(await screen.findByText("tenant-globex-row")).toBeInTheDocument();

    expect(screen.queryByText("secret-of-tenant-acme")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Copy/ })).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("Key name")).toHaveValue("");
  });

  it("a reload scheduled after creating keys in the previous tenant never runs in it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubNetwork(keysPage, created);
    renderPage(AccessKeys);
    expect(await screen.findByText("tenant-acme-row")).toBeInTheDocument();

    // Two keys in quick succession: each schedules a delayed reload.
    await createKey("first");
    await createKey("second");
    await switchToGlobex();
    expect(await screen.findByText("tenant-globex-row")).toBeInTheDocument();

    sent.length = 0;
    await act(async () => {
      vi.advanceTimersByTime(5000);
    });

    expect(sent.filter((r) => r.tenant === "tenant-acme")).toEqual([]);
    expect(screen.queryByText("tenant-acme-row")).not.toBeInTheDocument();
  });
});
