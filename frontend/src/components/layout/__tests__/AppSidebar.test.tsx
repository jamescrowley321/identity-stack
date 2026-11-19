import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AppSidebar } from "../AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { CAPABILITY_SETS, capabilitiesValue, type CapabilitySetName } from "@/test/capabilitySets";

// Mock useRBAC
const mockUseRBAC = vi.fn();
vi.mock("@/hooks/useRBAC", () => ({
  useRBAC: () => mockUseRBAC(),
}));

const mockUseCapabilities = vi.fn();
vi.mock("@/contexts/CapabilitiesContext", () => ({
  useCapabilities: () => mockUseCapabilities(),
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

/** The label of every link the sidebar shows. */
function shownLinks(): Set<string> {
  const sidebar = document.querySelector("[data-slot='sidebar']") as HTMLElement;
  return new Set(within(sidebar).getAllByRole("link").map((link) => link.textContent ?? ""));
}

const ALWAYS_FOR_ADMIN = [
  "Dashboard",
  "Members",
  "Roles",
  "Providers",
  "Sync Dashboard",
  "Events",
  "Provisional Users",
  "Profile",
];

describe("AppSidebar", () => {
  beforeEach(() => {
    mockUseCapabilities.mockReturnValue(capabilitiesValue(CAPABILITY_SETS.descope));
  });

  it("renders group labels for admin users", () => {
    mockUseRBAC.mockReturnValue({ isAdmin: true });
    renderSidebar();

    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.getByText("Platform")).toBeInTheDocument();
    expect(screen.getByText("Tenant")).toBeInTheDocument();
  });

  it("shows all nav items for admin users", () => {
    mockUseRBAC.mockReturnValue({ isAdmin: true });
    renderSidebar();

    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Members")).toBeInTheDocument();
    expect(screen.getByText("Roles")).toBeInTheDocument();
    expect(screen.getByText("Access Keys")).toBeInTheDocument();
    expect(screen.getByText("FGA")).toBeInTheDocument();
    expect(screen.getByText("Providers")).toBeInTheDocument();
    expect(screen.getByText("Sync Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Events")).toBeInTheDocument();
    expect(screen.getByText("Provisional Users")).toBeInTheDocument();
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
    expect(screen.queryByText("FGA")).not.toBeInTheDocument();
    expect(screen.queryByText("Providers")).not.toBeInTheDocument();
    expect(screen.queryByText("Sync Dashboard")).not.toBeInTheDocument();
    expect(screen.queryByText("Events")).not.toBeInTheDocument();
    expect(screen.queryByText("Provisional Users")).not.toBeInTheDocument();
    expect(screen.getByText("Tenant Settings")).toBeInTheDocument();
    expect(screen.getByText("Profile")).toBeInTheDocument();
  });

  it("hides Platform group for non-admin users", () => {
    mockUseRBAC.mockReturnValue({ isAdmin: false });
    renderSidebar();

    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.queryByText("Platform")).not.toBeInTheDocument();
    expect(screen.getByText("Tenant")).toBeInTheDocument();
  });

  it("renders app name in header", () => {
    mockUseRBAC.mockReturnValue({ isAdmin: false });
    renderSidebar();
    expect(screen.getByText("Descope Starter")).toBeInTheDocument();
  });
});

describe("AppSidebar shows the pages the deployment's capabilities give", () => {
  const cases: { set: CapabilitySetName; admin: string[]; member: string[] }[] = [
    {
      set: "descope",
      admin: [...ALWAYS_FOR_ADMIN, "Access Keys", "FGA", "Tenant Settings"],
      member: ["Dashboard", "Profile", "Tenant Settings"],
    },
    { set: "none", admin: ALWAYS_FOR_ADMIN, member: ["Dashboard", "Profile"] },
    { set: "machineCredentialsOnly", admin: [...ALWAYS_FOR_ADMIN, "Access Keys"], member: ["Dashboard", "Profile"] },
    {
      set: "customAttributesOnly",
      admin: [...ALWAYS_FOR_ADMIN, "Tenant Settings"],
      member: ["Dashboard", "Profile", "Tenant Settings"],
    },
    { set: "relationshipStoreOnly", admin: [...ALWAYS_FOR_ADMIN, "FGA"], member: ["Dashboard", "Profile"] },
  ];

  it.each(cases)("$set, to an admin", ({ set, admin }) => {
    mockUseCapabilities.mockReturnValue(capabilitiesValue(CAPABILITY_SETS[set]));
    mockUseRBAC.mockReturnValue({ isAdmin: true });
    renderSidebar();

    expect(shownLinks()).toEqual(new Set(admin));
  });

  it.each(cases)("$set, to a member", ({ set, member }) => {
    mockUseCapabilities.mockReturnValue(capabilitiesValue(CAPABILITY_SETS[set]));
    mockUseRBAC.mockReturnValue({ isAdmin: false });
    renderSidebar();

    expect(shownLinks()).toEqual(new Set(member));
  });
});
