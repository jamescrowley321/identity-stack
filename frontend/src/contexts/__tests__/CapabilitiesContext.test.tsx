import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { CapabilitiesProvider, useCapabilities } from "../CapabilitiesContext";

const mockUseAuth = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockApiFetch = vi.fn();
vi.mock("../../hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: mockApiFetch }),
}));

function Consumer() {
  const { loading, capabilities, relationshipStore } = useCapabilities();
  return (
    <span data-testid="state">
      {JSON.stringify({ loading, capabilities: [...capabilities].sort(), relationshipStore })}
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

    expect(state()).toEqual({ loading: true, capabilities: [], relationshipStore: "none" });
    expect(mockApiFetch.mock.calls).toEqual([["/api/capabilities"]]);
  });

  it("serves what the backend reports", async () => {
    mockApiFetch.mockReturnValue(
      answer(200, { capabilities: ["custom_attributes", "machine_credentials"], relationship_store: "descope" }),
    );
    render(
      <CapabilitiesProvider>
        <Consumer />
      </CapabilitiesProvider>,
    );

    await waitFor(() =>
      expect(state()).toEqual({
        loading: false,
        capabilities: ["custom_attributes", "machine_credentials"],
        relationshipStore: "descope",
      }),
    );
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

    await waitFor(() => expect(state()).toEqual({ loading: false, capabilities: [], relationshipStore: "none" }));
  });

  it("throws outside a provider", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Consumer />)).toThrow("useCapabilities must be used within a CapabilitiesProvider");
  });
});
