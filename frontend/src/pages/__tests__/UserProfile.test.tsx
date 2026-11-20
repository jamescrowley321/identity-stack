import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import UserProfile from "../UserProfile";

const mockApiFetch = vi.fn();
vi.mock("@/hooks/useApiClient", () => ({
  useApiClient: () => ({ apiFetch: mockApiFetch }),
}));

const mockServes = vi.fn();
vi.mock("@/contexts/CapabilitiesContext", () => ({
  useCapabilities: () => ({ serves: mockServes }),
}));

const { toast } = vi.hoisted(() => ({ toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() } }));
vi.mock("sonner", () => ({ toast }));

const PROFILE = {
  user_id: "sub-1",
  name: "Ann Before",
  given_name: "Ann",
  family_name: "Before",
  email: "ann@example.test",
  custom_attributes: {},
};

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockServes.mockReturnValue(false);
});

describe("UserProfile", () => {
  it("saves the caller's names, for a provider without custom attributes too", async () => {
    const saved = { status: "updated", name: "Anne After", given_name: "Anne", family_name: "After" };
    mockApiFetch.mockImplementation(async (_path: string, init?: RequestInit) =>
      jsonResponse(init?.method === "PATCH" ? saved : PROFILE),
    );
    render(
      <MemoryRouter>
        <UserProfile />
      </MemoryRouter>,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Edit name" }));
    await user.clear(screen.getByLabelText("Given name"));
    await user.type(screen.getByLabelText("Given name"), "Anne");
    await user.clear(screen.getByLabelText("Family name"));
    await user.type(screen.getByLabelText("Family name"), "After");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.getByText("Anne After")).toBeInTheDocument());
    expect(mockApiFetch).toHaveBeenCalledWith("/api/profile", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ given_name: "Anne", family_name: "After" }),
    });
    expect(toast.success).toHaveBeenCalledWith("Name saved");
    expect(screen.queryByText("Custom Attributes")).not.toBeInTheDocument();
  });

  it("warns when the identity provider did not take the names", async () => {
    const saved = { ...PROFILE, status: "updated", provider_sync: "failed" };
    mockApiFetch.mockImplementation(async (_path: string, init?: RequestInit) =>
      jsonResponse(init?.method === "PATCH" ? saved : PROFILE),
    );
    render(
      <MemoryRouter>
        <UserProfile />
      </MemoryRouter>,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Edit name" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(toast.warning).toHaveBeenCalledWith("Name saved here, but the identity provider did not take it"),
    );
  });

  it("saves a custom attribute through the attributes route", async () => {
    mockServes.mockReturnValue(true);
    mockApiFetch.mockImplementation(async (_path: string, init?: RequestInit) =>
      jsonResponse(init?.method === "PATCH" ? { status: "updated", key: "department", value: "Eng" } : PROFILE),
    );
    render(
      <MemoryRouter>
        <UserProfile />
      </MemoryRouter>,
    );
    const user = userEvent.setup();

    await screen.findByText("Custom Attributes");
    const department = screen.getByRole("row", { name: /department/ });
    await user.click(within(department).getByRole("button", { name: "Edit" }));
    await user.type(screen.getByRole("textbox"), "Eng");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockApiFetch).toHaveBeenCalledWith("/api/profile/attributes", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "department", value: "Eng" }),
      }),
    );
  });
});
