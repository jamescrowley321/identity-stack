import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { RequirePermission } from "../RequirePermission";

const mockUseRBAC = vi.fn();
vi.mock("@/hooks/useRBAC", () => ({
  useRBAC: () => mockUseRBAC(),
}));

describe("RequirePermission", () => {
  it("renders children when user has permission", () => {
    mockUseRBAC.mockReturnValue({
      hasPermission: (p: string) => p === "docs.write",
    });
    render(
      <RequirePermission permission="docs.write">
        <span>Protected Content</span>
      </RequirePermission>
    );
    expect(screen.getByText("Protected Content")).toBeInTheDocument();
  });

  it("renders nothing when user lacks permission", () => {
    mockUseRBAC.mockReturnValue({
      hasPermission: () => false,
    });
    const { container } = render(
      <RequirePermission permission="docs.write">
        <span>Protected Content</span>
      </RequirePermission>
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders fallback when user lacks permission and fallback provided", () => {
    mockUseRBAC.mockReturnValue({
      hasPermission: () => false,
    });
    render(
      <RequirePermission permission="docs.write" fallback={<span>No Access</span>}>
        <span>Protected Content</span>
      </RequirePermission>
    );
    expect(screen.queryByText("Protected Content")).not.toBeInTheDocument();
    expect(screen.getByText("No Access")).toBeInTheDocument();
  });
});
