import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { fireEvent } from "@testing-library/react";
import { toast } from "sonner";
import type { ComponentType } from "react";
import MemberManagement from "../MemberManagement";
import AccessKeys from "../AccessKeys";

// The page's apiFetch, swapped between renders as the active tenant changes it.
let currentFetch: (path: string, init?: RequestInit) => Promise<Response>;
vi.mock("@/hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: currentFetch }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

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
  path: string;
  body: (row: string) => unknown;
}

const pages: ListPage[] = [
  {
    name: "MemberManagement",
    Page: MemberManagement,
    path: "/api/members",
    body: (row) => ({ members: [{ userId: `${row}-id`, name: row, status: "enabled", roleNames: [] }] }),
  },
  {
    name: "AccessKeys",
    Page: AccessKeys,
    path: "/api/keys",
    body: (row) => ({ keys: [{ id: `${row}-id`, name: row, status: "active" }] }),
  },
];

function renderIn(Page: ComponentType) {
  const view = render(
    <MemoryRouter>
      <Page />
    </MemoryRouter>,
  );
  /** Switch tenant: the page gets a new apiFetch and loads again. */
  const switchTo = (fetchForTenant: typeof currentFetch) => {
    currentFetch = fetchForTenant;
    view.rerender(
      <MemoryRouter>
        <Page />
      </MemoryRouter>,
    );
  };
  return switchTo;
}

describe.each(pages)("$name after a tenant switch", ({ Page, path, body }) => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("drops the previous tenant's answer when it arrives after the new tenant's", async () => {
    const acmeAnswer = deferred();
    currentFetch = vi.fn((p: string) => (p === path ? acmeAnswer.promise : Promise.resolve(json(200, {}))));
    const switchTo = renderIn(Page);

    switchTo(vi.fn((p: string) => Promise.resolve(p === path ? json(200, body("globex-row")) : json(200, {}))));
    expect(await screen.findByText("globex-row")).toBeInTheDocument();

    await act(async () => {
      acmeAnswer.release(json(200, body("acme-row")));
    });

    expect(screen.queryByText("acme-row")).not.toBeInTheDocument();
    expect(screen.getByText("globex-row")).toBeInTheDocument();
  });

  it("empties the list when the new tenant's load is refused", async () => {
    currentFetch = vi.fn((p: string) => Promise.resolve(p === path ? json(200, body("acme-row")) : json(200, {})));
    const switchTo = renderIn(Page);
    expect(await screen.findByText("acme-row")).toBeInTheDocument();

    switchTo(vi.fn((p: string) => Promise.resolve(p === path ? json(403, { detail: "Forbidden" }) : json(200, {}))));

    await waitFor(() => expect(screen.queryByText("acme-row")).not.toBeInTheDocument());
  });
});

interface ReloadPage {
  name: string;
  Page: ComponentType;
  path: string;
  rowAction: string;
  loadError: string;
  body: (row: string) => unknown;
}

const reloadPages: ReloadPage[] = [
  { ...pages.find((p) => p.name === "MemberManagement")!, rowAction: "Remove", loadError: "Failed to load members" },
  { ...pages.find((p) => p.name === "AccessKeys")!, rowAction: "Delete", loadError: "Failed to load access keys" },
];

describe.each(reloadPages)("$name when a reload fails", ({ Page, path, rowAction, loadError, body }) => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it.each([
    ["is answered with an error", () => Promise.resolve(json(500, { detail: "Internal Server Error" }))],
    ["does not reach the API", () => Promise.reject(new TypeError("Failed to fetch"))],
  ])("keeps the rows and says so when the reload %s", async (_how, failedLoad) => {
    let loads = 0;
    currentFetch = vi.fn((p: string, init?: RequestInit) => {
      if (p === path && (init?.method ?? "GET") === "GET") {
        loads += 1;
        return loads === 1 ? Promise.resolve(json(200, body("acme-row"))) : failedLoad();
      }
      return Promise.resolve(json(200, {}));
    });
    renderIn(Page);
    expect(await screen.findByText("acme-row")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: rowAction }));
    });

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(loadError));
    expect(screen.getByText("acme-row")).toBeInTheDocument();
  });
});
