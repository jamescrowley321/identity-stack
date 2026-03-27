import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import TenantSwitcher from "../TenantSwitcher";

const mockSigninRedirect = vi.fn();
vi.mock("react-oidc-context", () => ({
  useAuth: () => ({
    signinRedirect: mockSigninRedirect,
  }),
}));

const mockUseTenants = vi.fn();
vi.mock("@/hooks/useTenants", () => ({
  useTenants: () => mockUseTenants(),
}));

describe("TenantSwitcher", () => {
  it("shows 'No tenants' when user has no tenants", () => {
    mockUseTenants.mockReturnValue({ currentTenantId: null, tenants: [] });
    render(<TenantSwitcher />);
    expect(screen.getByText("No tenants")).toBeInTheDocument();
  });

  it("shows badge for single tenant", () => {
    mockUseTenants.mockReturnValue({
      currentTenantId: "tenant-1",
      tenants: [{ id: "tenant-1", roles: [], permissions: [] }],
    });
    render(<TenantSwitcher />);
    expect(screen.getByText("tenant-1")).toBeInTheDocument();
  });

  it("shows select for multiple tenants", () => {
    mockUseTenants.mockReturnValue({
      currentTenantId: "tenant-1",
      tenants: [
        { id: "tenant-1", roles: [], permissions: [] },
        { id: "tenant-2", roles: [], permissions: [] },
      ],
    });
    render(<TenantSwitcher />);
    // Select trigger should show current tenant value
    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });
});
