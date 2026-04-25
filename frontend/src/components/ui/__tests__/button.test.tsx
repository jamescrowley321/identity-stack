import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Button, buttonVariants } from "../button";

describe("Button", () => {
  it("renders with default variant and size", () => {
    render(<Button>Click me</Button>);
    const button = screen.getByRole("button", { name: "Click me" });
    expect(button).toBeInTheDocument();
    expect(button).toHaveAttribute("data-slot", "button");
    expect(button).toHaveAttribute("data-variant", "default");
    expect(button).toHaveAttribute("data-size", "default");
  });

  it("applies default size class h-9", () => {
    render(<Button>Default</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/\bh-9\b/);
  });

  it("applies xs size class h-7", () => {
    render(<Button size="xs">XS</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/\bh-7\b/);
  });

  it("applies sm size class h-8", () => {
    render(<Button size="sm">SM</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/\bh-8\b/);
  });

  it("applies lg size class h-10", () => {
    render(<Button size="lg">LG</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/\bh-10\b/);
  });

  it("applies icon size class size-9", () => {
    render(<Button size="icon">I</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/\bsize-9\b/);
  });

  it("applies icon-xs size class size-7", () => {
    render(<Button size="icon-xs">I</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/\bsize-7\b/);
  });

  it("applies icon-sm size class size-8", () => {
    render(<Button size="icon-sm">I</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/\bsize-8\b/);
  });

  it("applies icon-lg size class size-10", () => {
    render(<Button size="icon-lg">I</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toMatch(/\bsize-10\b/);
  });

  it("applies variant classes", () => {
    render(<Button variant="outline">Outline</Button>);
    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("data-variant", "outline");
    expect(button.className).toContain("border-border");
  });

  it("merges custom className", () => {
    render(<Button className="custom-class">Styled</Button>);
    const button = screen.getByRole("button");
    expect(button.className).toContain("custom-class");
  });

  it("renders as child component with asChild", () => {
    render(
      <Button asChild>
        <a href="/test">Link Button</a>
      </Button>
    );
    const link = screen.getByRole("link", { name: "Link Button" });
    expect(link).toBeInTheDocument();
    expect(link).toHaveAttribute("data-slot", "button");
  });

  it("exports buttonVariants for external use", () => {
    const classes = buttonVariants({ variant: "default", size: "default" });
    expect(classes).toMatch(/\bh-9\b/);
  });
});
