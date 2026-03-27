import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { PageHeader } from "../PageHeader";

describe("PageHeader", () => {
  it("renders title", () => {
    render(<PageHeader title="Test Page" />);
    expect(screen.getByText("Test Page")).toBeInTheDocument();
  });

  it("renders description when provided", () => {
    render(<PageHeader title="Title" description="Some description" />);
    expect(screen.getByText("Some description")).toBeInTheDocument();
  });

  it("does not render description when not provided", () => {
    const { container } = render(<PageHeader title="Title" />);
    expect(container.querySelectorAll("p")).toHaveLength(0);
  });

  it("renders children in actions slot", () => {
    render(
      <PageHeader title="Title">
        <button>Action</button>
      </PageHeader>
    );
    expect(screen.getByText("Action")).toBeInTheDocument();
  });

  it("renders separator", () => {
    const { container } = render(<PageHeader title="Title" />);
    expect(container.querySelector("[data-slot='separator']")).toBeInTheDocument();
  });
});
