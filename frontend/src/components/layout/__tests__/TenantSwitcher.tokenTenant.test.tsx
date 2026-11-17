import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import TenantSwitcher from "../TenantSwitcher";

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
  Element.prototype.hasPointerCapture = vi.fn(() => false);
  Element.prototype.releasePointerCapture = vi.fn();
});

const mockContext = vi.fn();
vi.mock("@/contexts/IdentityContext", () => ({
  useIdentityContext: () => mockContext(),
}));

const mockUseTenants = vi.fn();
vi.mock("@/hooks/useTenants", () => ({
  useTenants: () => mockUseTenants(),
}));

const TWO_TENANTS = {
  currentTenantId: "globex",
  tenants: [
    { id: "acme", name: "Acme", roles: [], permissions: [] },
    { id: "globex", name: "Globex", roles: [], permissions: ["documents.read"] },
  ],
};

function context(overrides: Record<string, unknown> = {}) {
  return {
    setCurrentTenantId: vi.fn(),
    signInToTenant: vi.fn(() => Promise.resolve()),
    signInAgain: vi.fn(() => Promise.resolve()),
    tenantSwitch: "client",
    tenantSwitchFailure: null,
    ...overrides,
  };
}

describe("TenantSwitcher for a tenant that comes from the token", () => {
  beforeEach(() => {
    mockContext.mockReset();
    mockUseTenants.mockReset();
  });

  it("signs in to the chosen tenant when switching takes a new token", () => {
    const ctx = context({ tenantSwitch: "sign-in" });
    mockContext.mockReturnValue(ctx);
    mockUseTenants.mockReturnValue(TWO_TENANTS);

    render(<TenantSwitcher />);
    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("Acme"));

    expect(ctx.signInToTenant).toHaveBeenCalledWith("acme");
    expect(ctx.setCurrentTenantId).not.toHaveBeenCalled();
  });

  it("shows the current tenant and offers no switch when switching is not configured", () => {
    mockContext.mockReturnValue(context({ tenantSwitch: "none" }));
    mockUseTenants.mockReturnValue(TWO_TENANTS);

    render(<TenantSwitcher />);

    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getByText("Globex")).toBeInTheDocument();
    expect(screen.queryByText("Acme")).not.toBeInTheDocument();
  });

  it("keeps the switch client-side when the tenant does not come from the token", () => {
    const ctx = context({ tenantSwitch: "client" });
    mockContext.mockReturnValue(ctx);
    mockUseTenants.mockReturnValue(TWO_TENANTS);

    render(<TenantSwitcher />);
    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("Acme"));

    expect(ctx.setCurrentTenantId).toHaveBeenCalledWith("acme");
    expect(ctx.signInToTenant).not.toHaveBeenCalled();
  });

  it("reports a switch whose token lacks the tenant asked for, without showing that tenant", () => {
    const ctx = context({
      tenantSwitch: "sign-in",
      tenantSwitchFailure: { requestedTenantId: "acme" },
    });
    mockContext.mockReturnValue(ctx);
    mockUseTenants.mockReturnValue({ currentTenantId: null, tenants: [] });

    render(<TenantSwitcher />);

    expect(screen.getByRole("alert")).toHaveTextContent("Could not switch to acme");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByText("No tenants")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));
    expect(ctx.signInAgain).toHaveBeenCalledWith();
  });

  it("keeps the current tenant and the switch beside a failed switch report", () => {
    const ctx = context({
      tenantSwitch: "sign-in",
      tenantSwitchFailure: { requestedTenantId: "acme" },
    });
    mockContext.mockReturnValue(ctx);
    mockUseTenants.mockReturnValue(TWO_TENANTS);

    render(<TenantSwitcher />);

    expect(screen.getByRole("alert")).toHaveTextContent("Could not switch to acme");
    expect(screen.getByRole("combobox")).toHaveTextContent("Globex");
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));
    expect(ctx.signInAgain).toHaveBeenCalledWith();

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("Acme"));
    expect(ctx.signInToTenant).toHaveBeenCalledWith("acme");
  });

  it("keeps the current tenant beside a failed switch report when no switch is offered", () => {
    mockContext.mockReturnValue(
      context({ tenantSwitch: "none", tenantSwitchFailure: { requestedTenantId: "acme" } }),
    );
    mockUseTenants.mockReturnValue(TWO_TENANTS);

    render(<TenantSwitcher />);

    expect(screen.getByRole("alert")).toHaveTextContent("Could not switch to acme");
    expect(screen.getByText("Globex")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("reports a failed sign-in request instead of switching", async () => {
    const ctx = context({
      tenantSwitch: "sign-in",
      signInToTenant: vi.fn(() => Promise.reject(new Error("not a scope token"))),
    });
    mockContext.mockReturnValue(ctx);
    mockUseTenants.mockReturnValue(TWO_TENANTS);

    render(<TenantSwitcher />);
    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("Acme"));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Could not switch to acme"),
    );
  });

  it("keeps the current tenant and the switch after a failed sign-in request, and logs why", async () => {
    const reason = new Error("not a scope token");
    const signInToTenant = vi
      .fn()
      .mockImplementationOnce(() => Promise.reject(reason))
      .mockImplementation(() => Promise.resolve());
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const ctx = context({ tenantSwitch: "sign-in", signInToTenant });
    mockContext.mockReturnValue(ctx);
    mockUseTenants.mockReturnValue(TWO_TENANTS);

    render(<TenantSwitcher />);
    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("Acme"));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Could not switch to acme"),
    );
    expect(logged).toHaveBeenCalledWith("Tenant sign-in request failed", "acme", reason);
    expect(screen.getByRole("combobox")).toHaveTextContent("Globex");

    fireEvent.click(screen.getByRole("combobox"));
    fireEvent.click(screen.getByText("Acme"));

    expect(signInToTenant).toHaveBeenLastCalledWith("acme");
    expect(signInToTenant).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    logged.mockRestore();
  });

  it("logs a failed request to sign in again", async () => {
    const reason = new Error("metadata unreachable");
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const ctx = context({
      tenantSwitch: "sign-in",
      tenantSwitchFailure: { requestedTenantId: "acme" },
      signInAgain: vi.fn(() => Promise.reject(reason)),
    });
    mockContext.mockReturnValue(ctx);
    mockUseTenants.mockReturnValue({ currentTenantId: null, tenants: [] });

    render(<TenantSwitcher />);
    fireEvent.click(screen.getByRole("button", { name: "Sign in again" }));

    await waitFor(() =>
      expect(logged).toHaveBeenCalledWith("Sign-in request failed", reason),
    );
    logged.mockRestore();
  });
});
