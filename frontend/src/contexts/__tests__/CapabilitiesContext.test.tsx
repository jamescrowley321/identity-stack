import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { CapabilitiesProvider, useCapabilities } from "../CapabilitiesContext";
import { NO_CAPABILITIES, type Capabilities } from "../../lib/capabilities";
import { CAPABILITY_SETS, capabilitiesBody } from "@/test/capabilitySets";

/** What the consumer shows for a set. */
function shown(set: Capabilities, loading = false) {
  const { capabilities, relationship_store, sync_provider_principal } = capabilitiesBody(set);
  return { loading, capabilities, relationshipStore: relationship_store, syncProviderPrincipal: sync_provider_principal };
}

const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockApiFetch = vi.fn();
vi.mock("../../hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: mockApiFetch }),
}));

function Consumer() {
  const { loading, capabilities, relationshipStore, syncProviderPrincipal } = useCapabilities();
  return (
    <span data-testid="state">
      {JSON.stringify({ loading, capabilities: [...capabilities].sort(), relationshipStore, syncProviderPrincipal })}
    </span>
  );
}

function state() {
  return JSON.parse(screen.getByTestId("state").textContent ?? "");
}

function answer(status: number, body: unknown) {
  return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
}

describe("CapabilitiesProvider", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({ isAuthenticated: true, user: { profile: { sub: "user-1" } } });
  });

  it("serves nothing optional while it loads", () => {
    mockApiFetch.mockReturnValue(new Promise(() => {}));
    render(
      <CapabilitiesProvider>
        <Consumer />
      </CapabilitiesProvider>,
    );

    expect(state()).toEqual(shown(NO_CAPABILITIES, true));
    expect(mockApiFetch.mock.calls).toEqual([["/api/capabilities"]]);
  });

  it.each(["descope", "notTheSyncProvider"] as const)("serves what the backend reports for %s", async (name) => {
    mockApiFetch.mockReturnValue(answer(200, capabilitiesBody(CAPABILITY_SETS[name])));
    render(
      <CapabilitiesProvider>
        <Consumer />
      </CapabilitiesProvider>,
    );

    await waitFor(() => expect(state()).toEqual(shown(CAPABILITY_SETS[name])));
  });

  it.each([
    ["an error answer", () => answer(500, { detail: "boom" })],
    ["a failed request", () => Promise.reject(new Error("offline"))],
  ])("fails closed on %s", async (_name, response) => {
    mockApiFetch.mockImplementation(response);
    render(
      <CapabilitiesProvider>
        <Consumer />
      </CapabilitiesProvider>,
    );

    await waitFor(() => expect(state()).toEqual(shown(NO_CAPABILITIES)));
  });

  it("throws outside a provider", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Consumer />)).toThrow("useCapabilities must be used within a CapabilitiesProvider");
  });
});
