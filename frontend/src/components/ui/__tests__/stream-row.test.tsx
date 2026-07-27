import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StreamRow, type StreamEvent, type StreamVerb } from "../stream-row";

const baseEvent: StreamEvent = {
  timestamp: "12:04:31.220",
  verb: "create",
  subject: "user:auth0|abc123",
};

describe("StreamRow", () => {
  it("renders timestamp, verb, and subject", () => {
    render(<StreamRow event={baseEvent} />);
    expect(screen.getByText("12:04:31.220")).toBeInTheDocument();
    expect(screen.getByText("create")).toBeInTheDocument();
    expect(screen.getByText("user:auth0|abc123")).toBeInTheDocument();
  });

  it("applies data-slot attributes", () => {
    const { container } = render(<StreamRow event={baseEvent} />);
    expect(container.querySelector('[data-slot="stream-row"]')).toBeInTheDocument();
    expect(container.querySelector('[data-slot="stream-ts"]')).toBeInTheDocument();
    expect(container.querySelector('[data-slot="stream-icon"]')).toBeInTheDocument();
    expect(container.querySelector('[data-slot="stream-body"]')).toBeInTheDocument();
  });

  it("exposes the verb via data-verb", () => {
    const { container } = render(<StreamRow event={{ ...baseEvent, verb: "delete" }} />);
    expect(container.querySelector('[data-slot="stream-row"]')).toHaveAttribute(
      "data-verb",
      "delete",
    );
  });

  // AC: verb color-coded per verb. Mutation guard — each verb maps to a
  // distinct color class, so this fails if the verbClass map is collapsed.
  const verbColors: Record<StreamVerb, string> = {
    create: "text-success",
    update: "text-[oklch(0.45_0.18_235)]",
    delete: "text-destructive",
    skip: "text-muted-foreground",
  };

  it("color-codes each verb with a distinct class", () => {
    const classes = new Set<string>();
    for (const verb of Object.keys(verbColors) as StreamVerb[]) {
      render(<StreamRow event={{ ...baseEvent, verb }} />);
      const verbSpan = screen.getByText(verb);
      expect(verbSpan).toHaveClass(verbColors[verb]);
      classes.add(verbColors[verb]);
    }
    expect(classes.size).toBe(4);
  });

  it("renders the icon node when provided", () => {
    const { container } = render(
      <StreamRow event={{ ...baseEvent, icon: <svg data-testid="glyph" /> }} />,
    );
    const icon = container.querySelector('[data-slot="stream-icon"]');
    expect(icon?.querySelector('[data-testid="glyph"]')).toBeInTheDocument();
  });

  it("renders an empty icon slot when no icon is provided", () => {
    const { container } = render(<StreamRow event={baseEvent} />);
    const icon = container.querySelector('[data-slot="stream-icon"]');
    expect(icon).toBeInTheDocument();
    expect(icon).toBeEmptyDOMElement();
  });

  it("omits the code slot when code is absent", () => {
    const { container } = render(<StreamRow event={baseEvent} />);
    expect(container.querySelector('[data-slot="stream-code"]')).toBeNull();
  });

  it("renders the code slot when code is present", () => {
    const { container } = render(
      <StreamRow event={{ ...baseEvent, code: "200" }} />,
    );
    const code = container.querySelector('[data-slot="stream-code"]');
    expect(code).toBeInTheDocument();
    expect(code).toHaveTextContent("200");
  });

  it("does not let props override data-slot", () => {
    const { container } = render(
      <StreamRow event={baseEvent} data-slot="hacked" />,
    );
    expect(container.querySelector('[data-slot="stream-row"]')).toBeInTheDocument();
    expect(container.querySelector('[data-slot="hacked"]')).toBeNull();
  });

  it("merges a custom className", () => {
    const { container } = render(
      <StreamRow event={baseEvent} className="custom-row" />,
    );
    expect(container.querySelector('[data-slot="stream-row"]')).toHaveClass(
      "custom-row",
    );
  });
});
