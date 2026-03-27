import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AppSidebar } from "../AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";

// Mock useRBAC
const mockUseRBAC = vi.fn();
vi.mock("@/hooks/useRBAC", () => ({
  useRBAC: () => mockUseRBAC(),
}));

function renderSidebar(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SidebarProvider>
        <AppSidebar />
      </SidebarProvider>
    </MemoryRouter>
  );
}

describe("AppSidebar", () => {
  it("shows all nav items for admin users", () => {
    mockUseRBAC.mockReturnValue({ isAdmin: true });
    renderSidebar();

    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Members")).toBeInTheDocument();
    expect(screen.getByText("Roles")).toBeInTheDocument();
    expect(screen.getByText("Access Keys")).toBeInTheDocument();
    expect(screen.getByText("Tenant Settings")).toBeInTheDocument();
    expect(screen.getByText("Profile")).toBeInTheDocument();
  });

  it("hides admin-only items for non-admin users", () => {
    mockUseRBAC.mockReturnValue({ isAdmin: false });
    renderSidebar();

    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.queryByText("Members")).not.toBeInTheDocument();
    expect(screen.queryByText("Roles")).not.toBeInTheDocument();
    expect(screen.queryByText("Access Keys")).not.toBeInTheDocument();
    expect(screen.getByText("Tenant Settings")).toBeInTheDocument();
    expect(screen.getByText("Profile")).toBeInTheDocument();
  });

  it("renders app name in header", () => {
    mockUseRBAC.mockReturnValue({ isAdmin: false });
    renderSidebar();
    expect(screen.getByText("Descope Starter")).toBeInTheDocument();
  });
});
