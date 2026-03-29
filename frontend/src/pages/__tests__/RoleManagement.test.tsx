import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import RoleManagement from "../RoleManagement";

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
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

import { toast } from "sonner";

// --- Helpers ---

function rbacAdmin() {
  return {
    roles: ["admin"],
    permissions: ["docs.read"],
    isAdmin: true,
    isOwner: false,
    currentTenantId: "t1",
    hasRole: (r: string) => r === "admin",
    hasPermission: (p: string) => p === "docs.read",
  };
}

function rbacViewer() {
  return {
    roles: ["viewer"],
    permissions: [],
    isAdmin: false,
    isOwner: false,
    currentTenantId: "t1",
    hasRole: (r: string) => r === "viewer",
    hasPermission: () => false,
  };
}

function jsonResponse(data: unknown, ok = true, status = 200) {
  return Promise.resolve({
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: () => Promise.resolve(data),
  });
}

function setupAdminAPIMocks(
  roles = [
    { name: "admin", description: "Administrator", permissionNames: ["docs.read", "docs.write"] },
    { name: "viewer", description: "Read-only access", permissionNames: [] },
  ],
  permissions = [
    { name: "docs.read", description: "Read documents" },
    { name: "docs.write", description: "Write documents" },
  ],
) {
  mockApiFetch.mockImplementation((url: string) => {
    if (url === "/api/roles/me") return jsonResponse({ roles: ["admin"], permissions: ["docs.read"] });
    if (url === "/api/roles") return jsonResponse({ roles });
    if (url === "/api/permissions") return jsonResponse({ permissions });
    return jsonResponse({});
  });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <RoleManagement />
    </MemoryRouter>,
  );
}

// --- Tests ---

describe("RoleManagement", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // --- Non-admin view ---

  describe("non-admin user", () => {
    it("shows read-only alert and hides admin sections", async () => {
      mockUseRBAC.mockReturnValue(rbacViewer());
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/roles/me") return jsonResponse({ roles: ["viewer"], permissions: [] });
        return jsonResponse({});
      });
      renderPage();

      expect(screen.getByText(/need an admin or owner role/)).toBeInTheDocument();
      expect(screen.queryByText("Role Definitions")).not.toBeInTheDocument();
      expect(screen.queryByText("Permission Management")).not.toBeInTheDocument();
      expect(screen.queryByText("Manage User Roles")).not.toBeInTheDocument();
    });

    it("shows Your Roles card with user roles", () => {
      mockUseRBAC.mockReturnValue(rbacViewer());
      mockApiFetch.mockImplementation(() => jsonResponse({ roles: ["viewer"], permissions: [] }));
      renderPage();

      expect(screen.getByText("Your Roles")).toBeInTheDocument();
    });
  });

  // --- Admin view: loading and data display ---

  describe("admin user", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
    });

    it("shows loading skeletons initially", () => {
      // Never resolve the API calls so loading persists
      mockApiFetch.mockImplementation(() => new Promise(() => {}));
      renderPage();

      expect(screen.getByText("Role Definitions")).toBeInTheDocument();
      expect(screen.getByText("Permission Management")).toBeInTheDocument();
      // Both roles and permissions show "Loading..." in their CardDescription
      expect(screen.getAllByText("Loading...")).toHaveLength(2);
    });

    it("shows role definitions table with data", async () => {
      setupAdminAPIMocks();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });
      expect(screen.getByText("Administrator")).toBeInTheDocument();
      expect(screen.getByText("viewer")).toBeInTheDocument();
      expect(screen.getByText("Read-only access")).toBeInTheDocument();
      // Permission badges appear in multiple places (Your Roles card + role table + permissions table)
      expect(screen.getAllByText("docs.read").length).toBeGreaterThanOrEqual(1);
      expect(screen.getAllByText("docs.write").length).toBeGreaterThanOrEqual(1);
    });

    it("shows permission definitions table with data", async () => {
      setupAdminAPIMocks();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Read documents")).toBeInTheDocument();
      });
      expect(screen.getByText("Write documents")).toBeInTheDocument();
    });

    it("shows empty state when no roles defined", async () => {
      setupAdminAPIMocks([], []);
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("No roles defined yet.")).toBeInTheDocument();
      });
      expect(screen.getByText("No permissions defined yet.")).toBeInTheDocument();
    });

    it("populates role assignment dropdown from fetched roles", async () => {
      setupAdminAPIMocks();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Manage User Roles")).toBeInTheDocument();
      });
      // The select trigger should be present
      expect(screen.getByRole("combobox")).toBeInTheDocument();
    });
  });

  // --- Role CRUD ---

  describe("role CRUD", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      setupAdminAPIMocks();
    });

    it("opens create role dialog and submits", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Role"));

      expect(screen.getByText("Create Role", { selector: "[role='dialog'] *" })).toBeInTheDocument();
      expect(screen.getByLabelText("Name")).toBeInTheDocument();

      // Fill form
      await user.type(screen.getByLabelText("Name"), "editor");
      await user.type(screen.getByLabelText("Description"), "Can edit");

      // Submit
      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/roles") return jsonResponse({});
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() => {
        expect(toast.success).toHaveBeenCalledWith('Role "editor" created');
      });
    });

    it("opens edit role dialog with pre-filled data", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      // Click Edit on the first role row
      const editButtons = screen.getAllByText("Edit");
      await user.click(editButtons[0]);

      await waitFor(() => {
        expect(screen.getByText("Edit Role")).toBeInTheDocument();
      });

      // Check pre-filled values
      expect(screen.getByLabelText("Name")).toHaveValue("admin");
      expect(screen.getByLabelText("Description")).toHaveValue("Administrator");
    });

    it("calls PUT on role update", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      const editButtons = screen.getAllByText("Edit");
      await user.click(editButtons[0]);

      await waitFor(() => {
        expect(screen.getByText("Edit Role")).toBeInTheDocument();
      });

      // Modify description
      const descInput = screen.getByLabelText("Description");
      await user.clear(descInput);
      await user.type(descInput, "Updated admin");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "PUT" && url.includes("/api/roles/")) return jsonResponse({});
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Save Changes" }));

      await waitFor(() => {
        expect(toast.success).toHaveBeenCalledWith('Role "admin" updated');
      });
    });

    it("shows error toast on create role failure", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Role"));
      await user.type(screen.getByLabelText("Name"), "dupRole");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/roles")
          return jsonResponse({ detail: "Role already exists" }, false, 409);
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Role already exists");
      });
    });

    it("disables Create button when name is empty", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Role"));

      expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    });
  });

  // --- Permission CRUD ---

  describe("permission CRUD", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      setupAdminAPIMocks();
    });

    it("opens create permission dialog and submits", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Read documents")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Permission"));

      expect(screen.getByText("Create Permission", { selector: "[role='dialog'] *" })).toBeInTheDocument();

      await user.type(screen.getByLabelText("Name"), "billing.manage");
      await user.type(screen.getByLabelText("Description"), "Manage billing");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/permissions") return jsonResponse({});
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() => {
        expect(toast.success).toHaveBeenCalledWith('Permission "billing.manage" created');
      });
    });

    it("opens edit permission dialog with pre-filled data", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Read documents")).toBeInTheDocument();
      });

      // Find Edit buttons in the Permission Management section
      // Permission table Edit buttons come after Role table Edit buttons
      const editButtons = screen.getAllByText("Edit");
      // roles table has 2 edit buttons (admin, viewer), permission table has 2 (docs.read, docs.write)
      await user.click(editButtons[2]);

      await waitFor(() => {
        expect(screen.getByText("Edit Permission")).toBeInTheDocument();
      });

      expect(screen.getByLabelText("Name")).toHaveValue("docs.read");
      expect(screen.getByLabelText("Description")).toHaveValue("Read documents");
    });

    it("shows error toast on permission creation failure", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Read documents")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Permission"));
      await user.type(screen.getByLabelText("Name"), "docs.read");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/permissions")
          return jsonResponse({ detail: "Permission already exists" }, false, 409);
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Permission already exists");
      });
    });
  });

  // --- Delete confirmation ---

  describe("delete confirmation dialog", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      setupAdminAPIMocks();
    });

    it("shows confirmation dialog when deleting a role", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      const deleteButtons = screen.getAllByText("Delete");
      await user.click(deleteButtons[0]);

      await waitFor(() => {
        expect(screen.getByText("Confirm Delete")).toBeInTheDocument();
      });
      expect(screen.getByText(/Are you sure you want to delete the role/)).toBeInTheDocument();
    });

    it("deletes role on confirm", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      const deleteButtons = screen.getAllByText("Delete");
      await user.click(deleteButtons[0]);

      await waitFor(() => {
        expect(screen.getByText("Confirm Delete")).toBeInTheDocument();
      });

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "DELETE" && url.includes("/api/roles/")) return jsonResponse({});
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      // Click the destructive Delete button in the dialog
      const dialogDeleteBtn = screen.getAllByRole("button", { name: "Delete" });
      // The last Delete button should be the dialog's confirm button
      await user.click(dialogDeleteBtn[dialogDeleteBtn.length - 1]);

      await waitFor(() => {
        expect(toast.success).toHaveBeenCalledWith('Role "admin" deleted');
      });
    });

    it("cancels delete without calling API", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      const deleteButtons = screen.getAllByText("Delete");
      await user.click(deleteButtons[0]);

      await waitFor(() => {
        expect(screen.getByText("Confirm Delete")).toBeInTheDocument();
      });

      await user.click(screen.getByRole("button", { name: "Cancel" }));

      // Confirm no DELETE call was made
      const deleteCalls = mockApiFetch.mock.calls.filter(
        (call) => (call[1] as RequestInit | undefined)?.method === "DELETE",
      );
      expect(deleteCalls).toHaveLength(0);
    });

    it("shows confirmation dialog when deleting a permission", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Read documents")).toBeInTheDocument();
      });

      // Permission delete buttons come after role delete buttons
      const deleteButtons = screen.getAllByText("Delete");
      // roles: 2 delete buttons, permissions: 2 delete buttons → click 3rd (first permission)
      await user.click(deleteButtons[2]);

      await waitFor(() => {
        expect(screen.getByText("Confirm Delete")).toBeInTheDocument();
      });
      expect(screen.getByText(/Are you sure you want to delete the permission/)).toBeInTheDocument();
    });
  });

  // --- Permission multi-select in role dialog ---

  describe("permission multi-select", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      setupAdminAPIMocks();
    });

    it("shows permission checkboxes in create role dialog", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Role"));

      await waitFor(() => {
        expect(screen.getByRole("checkbox", { name: "docs.read" })).toBeInTheDocument();
      });
      expect(screen.getByRole("checkbox", { name: "docs.write" })).toBeInTheDocument();
    });

    it("toggles permission checkboxes", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Role"));

      const checkbox = screen.getByRole("checkbox", { name: "docs.read" });
      expect(checkbox).not.toBeChecked();

      await user.click(checkbox);
      expect(checkbox).toBeChecked();

      await user.click(checkbox);
      expect(checkbox).not.toBeChecked();
    });

    it("pre-selects permissions when editing a role", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      // Edit the admin role (has docs.read and docs.write)
      const editButtons = screen.getAllByText("Edit");
      await user.click(editButtons[0]);

      await waitFor(() => {
        expect(screen.getByText("Edit Role")).toBeInTheDocument();
      });

      expect(screen.getByRole("checkbox", { name: "docs.read" })).toBeChecked();
      expect(screen.getByRole("checkbox", { name: "docs.write" })).toBeChecked();
    });
  });

  // --- Role assignment ---

  describe("role assignment", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      setupAdminAPIMocks();
    });

    it("calls assign endpoint with user ID and selected role", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByPlaceholderText("User ID (login ID)")).toBeInTheDocument();
      });

      await user.type(screen.getByPlaceholderText("User ID (login ID)"), "user@test.com");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/roles/assign") return jsonResponse({});
        if (url === "/api/roles") return jsonResponse({ roles: [{ name: "admin", description: "", permissionNames: [] }] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Assign" }));

      await waitFor(() => {
        expect(toast.success).toHaveBeenCalled();
      });
    });

    it("disables assign/remove buttons when user ID is empty", async () => {
      renderPage();

      await waitFor(() => {
        expect(screen.getByRole("button", { name: "Assign" })).toBeDisabled();
      });
      expect(screen.getByRole("button", { name: "Remove" })).toBeDisabled();
    });
  });

  // --- Network error handling ---

  describe("network errors", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
    });

    it("shows error toast when role creation throws", async () => {
      setupAdminAPIMocks();
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Role"));
      await user.type(screen.getByLabelText("Name"), "newrole");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST") return Promise.reject(new Error("Network error"));
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to create role");
      });
    });

    it("shows error toast when permission creation throws", async () => {
      setupAdminAPIMocks();
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Read documents")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Permission"));
      await user.type(screen.getByLabelText("Name"), "newperm");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST") return Promise.reject(new Error("Network error"));
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to create permission");
      });
    });

    it("shows error toast when role loading fails", async () => {
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        if (url === "/api/roles") return Promise.reject(new Error("Network error"));
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        return jsonResponse({});
      });
      renderPage();

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to load roles");
      });
    });

    it("shows error toast when permission loading fails", async () => {
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return Promise.reject(new Error("Network error"));
        return jsonResponse({});
      });
      renderPage();

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to load permissions");
      });
    });

    it("handles non-JSON error response gracefully", async () => {
      setupAdminAPIMocks();
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("admin")).toBeInTheDocument();
      });

      await user.click(screen.getByText("Create Role"));
      await user.type(screen.getByLabelText("Name"), "badrole");

      // Return a non-JSON error response
      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/roles")
          return Promise.resolve({
            ok: false,
            status: 502,
            statusText: "Bad Gateway",
            json: () => Promise.reject(new Error("not JSON")),
          });
        if (url === "/api/roles") return jsonResponse({ roles: [] });
        if (url === "/api/permissions") return jsonResponse({ permissions: [] });
        if (url === "/api/roles/me") return jsonResponse({ roles: [], permissions: [] });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Create" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to create role");
      });
    });
  });
});
