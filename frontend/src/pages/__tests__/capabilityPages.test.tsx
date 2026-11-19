import { describe, it, expect, vi, beforeEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { CapabilityRoute } from "@/App";
import UserProfile from "@/pages/UserProfile";
import { CAPABILITY_SETS, capabilitiesValue, type CapabilitySetName } from "@/test/capabilitySets";

const mockUseCapabilities = vi.fn();
vi.mock("@/contexts/CapabilitiesContext", () => ({
  useCapabilities: () => mockUseCapabilities(),
}));

const mockApiFetch = vi.fn();
vi.mock("@/hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: mockApiFetch }),
}));

vi.mock("react-oidc-context", () => ({ useAuth: () => ({ isAuthenticated: true }) }));

const GATED_PAGES = ["/keys", "/fga", "/settings"] as const;

/** Each capability set and which of the gated pages it gives. */
const GIVES: Record<CapabilitySetName, Record<(typeof GATED_PAGES)[number], boolean>> = {
  descope: { "/keys": true, "/fga": true, "/settings": true },
  none: { "/keys": false, "/fga": false, "/settings": false },
  machineCredentialsOnly: { "/keys": true, "/fga": false, "/settings": false },
  customAttributesOnly: { "/keys": false, "/fga": false, "/settings": true },
  relationshipStoreOnly: { "/keys": false, "/fga": true, "/settings": false },
};

function landing(path: string): string {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/" element={<p>dashboard</p>} />
        {GATED_PAGES.map((page) => (
          <Route
            key={page}
            path={page}
            element={
              <CapabilityRoute path={page}>
                <p>page {page}</p>
              </CapabilityRoute>
            }
          />
        ))}
      </Routes>
    </MemoryRouter>,
  );
  const landed = screen.getByText(/^(dashboard|page .*)$/).textContent ?? "";
  cleanup();
  return landed;
}

describe.each(Object.keys(GIVES) as CapabilitySetName[])("with the %s capabilities", (name) => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseCapabilities.mockReturnValue(capabilitiesValue(CAPABILITY_SETS[name]));
  });

  it("a gated page linked straight to opens only where its capability is", () => {
    const landed = Object.fromEntries(GATED_PAGES.map((page) => [page, landing(page)]));

    expect(landed).toEqual(
      Object.fromEntries(GATED_PAGES.map((page) => [page, GIVES[name][page] ? `page ${page}` : "dashboard"])),
    );
  });

  it("the profile shows custom attributes only with the custom_attributes capability", async () => {
    mockApiFetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({ user_id: "u1", name: "Ada", email: "ada@example.test", custom_attributes: {} }),
    });
    render(
      <MemoryRouter>
        <UserProfile />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText("Ada")).toBeInTheDocument());
    expect(screen.queryByText("Custom Attributes") !== null).toBe(GIVES[name]["/settings"]);
  });
});

