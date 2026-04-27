import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge, badgeVariants } from "../badge";

describe("Badge", () => {
  it("renders with default variant", () => {
    render(<Badge>Status</Badge>);
    const badge = screen.getByText("Status");
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute("data-slot", "badge");
    expect(badge).toHaveAttribute("data-variant", "default");
  });

  it("renders success variant with green tones", () => {
    render(<Badge variant="success">Synced</Badge>);
    const badge = screen.getByText("Synced");
    expect(badge).toHaveAttribute("data-variant", "success");
    expect(badge.className).toContain("text-success");
    expect(badge.className).toContain("bg-success/20");
  });

  it("renders warning variant with amber tones", () => {
    render(<Badge variant="warning">Pending</Badge>);
    const badge = screen.getByText("Pending");
    expect(badge).toHaveAttribute("data-variant", "warning");
    expect(badge.className).toContain("text-warning");
    expect(badge.className).toContain("bg-warning/25");
  });

  it("renders destructive variant", () => {
    render(<Badge variant="destructive">Error</Badge>);
    const badge = screen.getByText("Error");
    expect(badge).toHaveAttribute("data-variant", "destructive");
    expect(badge.className).toContain("text-destructive");
  });

  it("merges custom className", () => {
    render(<Badge className="custom-class">Styled</Badge>);
    const badge = screen.getByText("Styled");
    expect(badge.className).toContain("custom-class");
  });

  it("renders as child component with asChild", () => {
    render(
      <Badge asChild>
        <a href="/test">Link Badge</a>
      </Badge>
    );
    const link = screen.getByRole("link", { name: "Link Badge" });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("data-slot", "badge");
  });

  it("exports badgeVariants for external use", () => {
    const classes = badgeVariants({ variant: "success" });
    expect(classes).toContain("text-success");
  });
});
