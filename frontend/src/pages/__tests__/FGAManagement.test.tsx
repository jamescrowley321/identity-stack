import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import FGAManagement from "../FGAManagement";

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

/** Get an input element by its HTML id attribute. */
function getInput(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`Input #${id} not found`);
  return el;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <FGAManagement />
    </MemoryRouter>,
  );
}

// --- Tests ---

describe("FGAManagement", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // --- Non-admin view ---

  describe("non-admin user", () => {
    it("shows access denied alert and hides admin sections", () => {
      mockUseRBAC.mockReturnValue(rbacViewer());
      renderPage();

      expect(screen.getByText(/need an admin or owner role/)).toBeInTheDocument();
      expect(screen.queryByText("Authorization Schema")).not.toBeInTheDocument();
      expect(screen.queryByText("Relations")).not.toBeInTheDocument();
      expect(screen.queryByText("Create Relation")).not.toBeInTheDocument();
      expect(screen.queryByText("Authorization Check")).not.toBeInTheDocument();
    });
  });

  // --- Admin view: schema loading ---

  describe("admin user - schema", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
    });

    it("shows loading skeletons initially", () => {
      mockApiFetch.mockImplementation(() => new Promise(() => {}));
      renderPage();

      expect(screen.getByText("Authorization Schema")).toBeInTheDocument();
    });

    it("displays schema content when loaded", async () => {
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/fga/schema") return jsonResponse({ schema: "type document {\n  relation viewer: user\n}" });
        return jsonResponse({});
      });
      renderPage();

      await waitFor(() => {
        expect(screen.getByText(/type document/)).toBeInTheDocument();
      });
    });

    it("shows empty state when no schema defined", async () => {
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/fga/schema") return jsonResponse({ schema: "" });
        return jsonResponse({});
      });
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("No schema defined.")).toBeInTheDocument();
      });
    });

    it("shows error toast on schema load failure", async () => {
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/fga/schema") return Promise.reject(new Error("Network error"));
        return jsonResponse({});
      });
      renderPage();

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to load FGA schema");
      });
    });

    it("shows error toast on non-ok schema response", async () => {
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/fga/schema") return jsonResponse(null, false, 502);
        return jsonResponse({});
      });
      renderPage();

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to load FGA schema");
      });
    });
  });

  // --- Browse relations ---

  describe("admin user - browse relations", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });
    });

    it("Browse button is disabled when inputs are empty", () => {
      renderPage();

      const browseBtn = screen.getByRole("button", { name: "Browse" });
      expect(browseBtn).toBeDisabled();
    });

    it("fetches and displays relations on browse", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Schema")).toBeInTheDocument();
      });

      await user.type(getInput("rel-resource-type"), "document");
      await user.type(getInput("rel-resource-id"), "doc-1");

      mockApiFetch.mockImplementation((url: string) => {
        if (typeof url === "string" && url.includes("/api/fga/relations")) {
          return jsonResponse({
            relations: [
              { relationDefinition: "owner", target: "user:u1" },
              { relationDefinition: "viewer", target: "user:u2" },
            ],
          });
        }
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Browse" }));

      await waitFor(() => {
        expect(screen.getByText("owner")).toBeInTheDocument();
      });
      expect(screen.getByText("user:u1")).toBeInTheDocument();
      expect(screen.getByText("viewer")).toBeInTheDocument();
      expect(screen.getByText("user:u2")).toBeInTheDocument();
    });

    it("shows empty message when no relations found", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Schema")).toBeInTheDocument();
      });

      await user.type(getInput("rel-resource-type"), "document");
      await user.type(getInput("rel-resource-id"), "doc-999");

      mockApiFetch.mockImplementation((url: string) => {
        if (typeof url === "string" && url.includes("/api/fga/relations")) {
          return jsonResponse({ relations: [] });
        }
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Browse" }));

      await waitFor(() => {
        expect(screen.getByText("No relations found.")).toBeInTheDocument();
      });
    });

    it("shows error toast on browse failure", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Schema")).toBeInTheDocument();
      });

      await user.type(getInput("rel-resource-type"), "document");
      await user.type(getInput("rel-resource-id"), "doc-1");

      mockApiFetch.mockImplementation((url: string) => {
        if (typeof url === "string" && url.includes("/api/fga/relations")) {
          return Promise.reject(new Error("Network error"));
        }
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Browse" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to load relations");
      });
    });

    it("shows API error detail on non-ok browse response", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Schema")).toBeInTheDocument();
      });

      await user.type(getInput("rel-resource-type"), "document");
      await user.type(getInput("rel-resource-id"), "doc-1");

      mockApiFetch.mockImplementation((url: string) => {
        if (typeof url === "string" && url.includes("/api/fga/relations")) {
          return jsonResponse({ detail: "Invalid resource type" }, false, 400);
        }
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Browse" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Invalid resource type");
      });
    });
  });

  // --- Delete relation ---

  describe("admin user - delete relation", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      vi.spyOn(window, "confirm").mockReturnValue(true);
    });

    it("deletes a relation and refreshes the list", async () => {
      const user = userEvent.setup();

      let browseCount = 0;
      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        if (typeof url === "string" && url.includes("/api/fga/relations") && (!opts || !opts.method || opts.method === "GET")) {
          browseCount++;
          if (browseCount <= 1) {
            return jsonResponse({
              relations: [{ relationDefinition: "owner", target: "user:u1" }],
            });
          }
          return jsonResponse({ relations: [] });
        }
        if (opts?.method === "DELETE") return jsonResponse({ status: "deleted" });
        return jsonResponse({});
      });

      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Schema")).toBeInTheDocument();
      });

      await user.type(getInput("rel-resource-type"), "document");
      await user.type(getInput("rel-resource-id"), "doc-1");
      await user.click(screen.getByRole("button", { name: "Browse" }));

      await waitFor(() => {
        expect(screen.getByText("owner")).toBeInTheDocument();
      });

      await user.click(screen.getByRole("button", { name: "Delete" }));

      await waitFor(() => {
        expect(toast.success).toHaveBeenCalledWith("Relation deleted");
      });
    });

    it("does not delete when confirm is cancelled", async () => {
      vi.spyOn(window, "confirm").mockReturnValue(false);
      const user = userEvent.setup();

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        if (typeof url === "string" && url.includes("/api/fga/relations") && (!opts || !opts.method || opts.method === "GET")) {
          return jsonResponse({
            relations: [{ relationDefinition: "owner", target: "user:u1" }],
          });
        }
        return jsonResponse({});
      });

      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Schema")).toBeInTheDocument();
      });

      await user.type(getInput("rel-resource-type"), "document");
      await user.type(getInput("rel-resource-id"), "doc-1");
      await user.click(screen.getByRole("button", { name: "Browse" }));

      await waitFor(() => {
        expect(screen.getByText("owner")).toBeInTheDocument();
      });

      await user.click(screen.getByRole("button", { name: "Delete" }));

      // No DELETE call should have been made
      const deleteCalls = mockApiFetch.mock.calls.filter(
        ([, opts]: [string, RequestInit | undefined]) => opts?.method === "DELETE",
      );
      expect(deleteCalls).toHaveLength(0);
    });

    it("shows error toast on delete failure", async () => {
      const user = userEvent.setup();

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        if (typeof url === "string" && url.includes("/api/fga/relations") && (!opts || !opts.method || opts.method === "GET")) {
          return jsonResponse({
            relations: [{ relationDefinition: "owner", target: "user:u1" }],
          });
        }
        if (opts?.method === "DELETE") return jsonResponse({ detail: "Not found" }, false, 404);
        return jsonResponse({});
      });

      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Schema")).toBeInTheDocument();
      });

      await user.type(getInput("rel-resource-type"), "document");
      await user.type(getInput("rel-resource-id"), "doc-1");
      await user.click(screen.getByRole("button", { name: "Browse" }));

      await waitFor(() => {
        expect(screen.getByText("owner")).toBeInTheDocument();
      });

      await user.click(screen.getByRole("button", { name: "Delete" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Not found");
      });
    });
  });

  // --- Create relation ---

  describe("admin user - create relation", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });
    });

    it("Create button is disabled when fields are empty", () => {
      renderPage();

      const createBtns = screen.getAllByRole("button", { name: "Create" });
      expect(createBtns[0]).toBeDisabled();
    });

    it("creates a relation and clears the form", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Create Relation")).toBeInTheDocument();
      });

      await user.type(getInput("create-resource-type"), "document");
      await user.type(getInput("create-resource-id"), "doc-1");
      await user.type(getInput("create-relation"), "viewer");
      await user.type(getInput("create-target"), "user:abc");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/fga/relations") return jsonResponse({}, true, 201);
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      const createBtns = screen.getAllByRole("button", { name: "Create" });
      await user.click(createBtns[0]);

      await waitFor(() => {
        expect(toast.success).toHaveBeenCalledWith("Relation created");
      });

      // Form should be cleared after successful creation
      expect(getInput("create-relation")).toHaveValue("");
      expect(getInput("create-target")).toHaveValue("");
    });

    it("shows error toast on create failure", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Create Relation")).toBeInTheDocument();
      });

      await user.type(getInput("create-resource-type"), "document");
      await user.type(getInput("create-resource-id"), "doc-1");
      await user.type(getInput("create-relation"), "viewer");
      await user.type(getInput("create-target"), "user:abc");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/fga/relations") {
          return jsonResponse({ detail: "Schema not configured" }, false, 400);
        }
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      const createBtns = screen.getAllByRole("button", { name: "Create" });
      await user.click(createBtns[0]);

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Schema not configured");
      });
    });

    it("shows generic error toast on network failure", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Create Relation")).toBeInTheDocument();
      });

      await user.type(getInput("create-resource-type"), "document");
      await user.type(getInput("create-resource-id"), "doc-1");
      await user.type(getInput("create-relation"), "viewer");
      await user.type(getInput("create-target"), "user:abc");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST") return Promise.reject(new Error("Network error"));
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      const createBtns = screen.getAllByRole("button", { name: "Create" });
      await user.click(createBtns[0]);

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to create relation");
      });
    });
  });

  // --- Authorization check ---

  describe("admin user - authorization check", () => {
    beforeEach(() => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      mockApiFetch.mockImplementation((url: string) => {
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });
    });

    it("Check button is disabled when fields are empty", () => {
      renderPage();

      expect(screen.getByRole("button", { name: "Check" })).toBeDisabled();
    });

    it("shows Allowed badge when permission is granted", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Check")).toBeInTheDocument();
      });

      await user.type(getInput("check-resource-type"), "document");
      await user.type(getInput("check-resource-id"), "doc-1");
      await user.type(getInput("check-relation"), "can_view");
      await user.type(getInput("check-target"), "user:abc");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/fga/check") return jsonResponse({ allowed: true });
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Check" }));

      await waitFor(() => {
        expect(screen.getByText("Allowed")).toBeInTheDocument();
      });
    });

    it("shows Denied badge when permission is denied", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Check")).toBeInTheDocument();
      });

      await user.type(getInput("check-resource-type"), "document");
      await user.type(getInput("check-resource-id"), "doc-1");
      await user.type(getInput("check-relation"), "can_edit");
      await user.type(getInput("check-target"), "user:xyz");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/fga/check") return jsonResponse({ allowed: false });
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Check" }));

      await waitFor(() => {
        expect(screen.getByText("Denied")).toBeInTheDocument();
      });
    });

    it("shows error toast on check failure", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Check")).toBeInTheDocument();
      });

      await user.type(getInput("check-resource-type"), "document");
      await user.type(getInput("check-resource-id"), "doc-1");
      await user.type(getInput("check-relation"), "can_view");
      await user.type(getInput("check-target"), "user:abc");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/fga/check") {
          return jsonResponse({ detail: "FGA not available" }, false, 502);
        }
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Check" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("FGA not available");
      });
    });

    it("shows generic error toast on network failure", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Check")).toBeInTheDocument();
      });

      await user.type(getInput("check-resource-type"), "document");
      await user.type(getInput("check-resource-id"), "doc-1");
      await user.type(getInput("check-relation"), "can_view");
      await user.type(getInput("check-target"), "user:abc");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST") return Promise.reject(new Error("Network error"));
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Check" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to check permission");
      });
    });

    it("handles non-JSON error response gracefully", async () => {
      const user = userEvent.setup();
      renderPage();

      await waitFor(() => {
        expect(screen.getByText("Authorization Check")).toBeInTheDocument();
      });

      await user.type(getInput("check-resource-type"), "document");
      await user.type(getInput("check-resource-id"), "doc-1");
      await user.type(getInput("check-relation"), "can_view");
      await user.type(getInput("check-target"), "user:abc");

      mockApiFetch.mockImplementation((url: string, opts?: RequestInit) => {
        if (opts?.method === "POST" && url === "/api/fga/check") {
          return Promise.resolve({
            ok: false,
            status: 502,
            statusText: "Bad Gateway",
            json: () => Promise.reject(new Error("not JSON")),
          });
        }
        if (url === "/api/fga/schema") return jsonResponse({ schema: "v1" });
        return jsonResponse({});
      });

      await user.click(screen.getByRole("button", { name: "Check" }));

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith("Failed to check permission");
      });
    });
  });

  // --- Page header ---

  describe("page header", () => {
    it("renders page title and description", () => {
      mockUseRBAC.mockReturnValue(rbacAdmin());
      mockApiFetch.mockImplementation(() => new Promise(() => {}));
      renderPage();

      expect(screen.getByText("FGA Management")).toBeInTheDocument();
    });
  });
});
