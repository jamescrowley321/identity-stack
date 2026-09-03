import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import TenantSwitcher from "../TenantSwitcher";

// Radix Select relies on pointer-capture + scrollIntoView APIs that jsdom
// does not implement; stub them so the dropdown opens under test.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
  Element.prototype.hasPointerCapture = vi.fn(() => false);
  Element.prototype.releasePointerCapture = vi.fn();
});

// TenantSwitcher now switches tenants via the client-side IdentityContext
// selection (setCurrentTenantId) — no signinRedirect / token re-issue, since
// canonical resolution is provider-neutral.
const mockSetCurrentTenantId = vi.fn();
vi.mock("@/contexts/IdentityContext", () => ({
  useIdentityContext: () => ({ setCurrentTenantId: mockSetCurrentTenantId }),
}));

const mockUseTenants = vi.fn();
vi.mock("@/hooks/useTenants", () => ({
  useTenants: () => mockUseTenants(),
}));

describe("TenantSwitcher", () => {
  beforeEach(() => {
    mockSetCurrentTenantId.mockReset();
    mockUseTenants.mockReset();
  });

  it("shows 'No tenants' when user has no tenants", () => {
    mockUseTenants.mockReturnValue({ currentTenantId: null, tenants: [] });
    render(<TenantSwitcher />);
    expect(screen.getByText("No tenants")).toBeInTheDocument();
  });

  it("shows badge with the tenant display name for a single tenant", () => {
    mockUseTenants.mockReturnValue({
      currentTenantId: "tenant-1",
      tenants: [{ id: "tenant-1", name: "Acme", roles: [], permissions: [] }],
    });
    render(<TenantSwitcher />);
    expect(screen.getByText("Acme")).toBeInTheDocument();
  });

  it("falls back to tenant id when name is empty", () => {
    mockUseTenants.mockReturnValue({
      currentTenantId: "tenant-1",
      tenants: [{ id: "tenant-1", name: "", roles: [], permissions: [] }],
    });
    render(<TenantSwitcher />);
    expect(screen.getByText("tenant-1")).toBeInTheDocument();
  });

  it("shows select for multiple tenants", () => {
    mockUseTenants.mockReturnValue({
      currentTenantId: "tenant-1",
      tenants: [
        { id: "tenant-1", name: "Acme", roles: [], permissions: [] },
        { id: "tenant-2", name: "Beta", roles: [], permissions: [] },
      ],
    });
    render(<TenantSwitcher />);
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("switches the active tenant via setCurrentTenantId (client-side, no token re-issue)", () => {
    mockUseTenants.mockReturnValue({
      currentTenantId: "tenant-1",
      tenants: [
        { id: "tenant-1", name: "Acme", roles: [], permissions: [] },
        { id: "tenant-2", name: "Beta", roles: [], permissions: [] },
      ],
    });
    render(<TenantSwitcher />);

    // Open the select and choose the other tenant.
    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("Beta"));

    expect(mockSetCurrentTenantId).toHaveBeenCalledWith("tenant-2");
  });
});
