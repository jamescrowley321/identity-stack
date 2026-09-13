import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import TenantSettings from "../TenantSettings";

// --- Mocks ---

const mockApiFetch = vi.fn();
vi.mock("@/hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: mockApiFetch }),
}));

const mockUseRBAC = vi.fn();
vi.mock("@/hooks/useRBAC", () => ({
  useRBAC: () => mockUseRBAC(),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

// --- Helpers ---

function rbacAdmin() {
  return {
    roles: ["admin"],
    permissions: [],
    isAdmin: true,
    isOwner: false,
    currentTenantId: "t1",
    hasRole: (r: string) => r === "admin",
    hasPermission: () => false,
  };
}

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <TenantSettings />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseRBAC.mockReturnValue(rbacAdmin());
});

describe("TenantSettings", () => {
  it("renders a tenant whose customAttributes are null", async () => {
    // Descope sends the key with a null value rather than omitting it when a
    // tenant carries no custom attributes. Reading through it unguarded threw
    // "Cannot read properties of null (reading 'plan_tier')", which unmounted
    // the whole app — the E2E suite saw an empty <div id="root"> and could say
    // only that the heading never appeared.
    mockApiFetch.mockResolvedValue(
      jsonResponse({ tenant_id: "t1", name: "Acme Corp", custom_attributes: null }),
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Tenant Settings", level: 1 })).toBeInTheDocument();
    });
    // Scoped to the Current Settings badge: the same words also appear in the
    // edit form's plan selector.
    expect(screen.getByText("free", { selector: '[data-slot="badge"]' })).toBeInTheDocument();
    expect(screen.getByText("Not set")).toBeInTheDocument();
  });

  it("renders the values a tenant does carry", async () => {
    mockApiFetch.mockResolvedValue(
      jsonResponse({
        tenant_id: "t1",
        name: "Acme Corp",
        custom_attributes: { plan_tier: "enterprise", max_members: 250 },
      }),
    );

    renderPage();

    await waitFor(() => {
      expect(
        screen.getByText("enterprise", { selector: '[data-slot="badge"]' }),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("250")).toBeInTheDocument();
  });
});
