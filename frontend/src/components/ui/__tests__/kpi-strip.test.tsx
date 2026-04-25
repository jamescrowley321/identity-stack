import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { KpiStrip, type KpiItem } from "../kpi-strip";

const baseItems: KpiItem[] = [
  { label: "Users", value: "1,234" },
  { label: "Syncs", value: 42, delta: "+12%", trend: "up" },
  { label: "Errors", value: 3, delta: "-8%", trend: "down" },
  { label: "Latency", value: "120ms", delta: "0%", trend: "neutral" },
];

describe("KpiStrip", () => {
  it("renders all items with labels and values", () => {
    render(<KpiStrip items={baseItems} />);
    expect(screen.getByText("Users")).toBeInTheDocument();
    expect(screen.getByText("1,234")).toBeInTheDocument();
    expect(screen.getByText("Syncs")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("Errors")).toBeInTheDocument();
    expect(screen.getByText("Latency")).toBeInTheDocument();
  });

  it("applies data-slot attributes", () => {
    const { container } = render(<KpiStrip items={baseItems} />);
    expect(container.querySelector('[data-slot="kpi-strip"]')).toBeInTheDocument();
    expect(container.querySelectorAll('[data-slot="kpi-item"]')).toHaveLength(4);
  });

  it("renders positive delta with success color", () => {
    const { container } = render(
      <KpiStrip items={[{ label: "Users", value: 10, delta: "+5%", trend: "up" }]} />
    );
    const delta = container.querySelector('[data-slot="kpi-delta"]');
    expect(delta).not.toBeNull();
    expect(delta).toHaveClass("text-success");
    expect(delta).toHaveTextContent("+5%");
  });

  it("renders negative delta with destructive color", () => {
    const { container } = render(
      <KpiStrip items={[{ label: "Errors", value: 3, delta: "-8%", trend: "down" }]} />
    );
    const delta = container.querySelector('[data-slot="kpi-delta"]');
    expect(delta).not.toBeNull();
    expect(delta).toHaveClass("text-destructive");
  });

  it("renders neutral delta with muted color", () => {
    const { container } = render(
      <KpiStrip items={[{ label: "Latency", value: "120ms", delta: "0%", trend: "neutral" }]} />
    );
    const delta = container.querySelector('[data-slot="kpi-delta"]');
    expect(delta).not.toBeNull();
    expect(delta).toHaveClass("text-muted-foreground");
  });

  it("uses muted color when trend is omitted", () => {
    const { container } = render(
      <KpiStrip items={[{ label: "Latency", value: "120ms", delta: "0%" }]} />
    );
    const delta = container.querySelector('[data-slot="kpi-delta"]');
    expect(delta).not.toBeNull();
    expect(delta).toHaveClass("text-muted-foreground");
  });

  it("omits delta element when delta is not provided", () => {
    const { container } = render(
      <KpiStrip items={[{ label: "Users", value: 100 }]} />
    );
    expect(container.querySelector('[data-slot="kpi-delta"]')).not.toBeInTheDocument();
  });

  it("renders empty container for empty items", () => {
    const { container } = render(<KpiStrip items={[]} />);
    const strip = container.querySelector('[data-slot="kpi-strip"]');
    expect(strip).not.toBeNull();
    expect(strip?.children).toHaveLength(0);
  });

  it("merges custom className", () => {
    const { container } = render(
      <KpiStrip items={baseItems} className="custom-class" />
    );
    const strip = container.querySelector('[data-slot="kpi-strip"]');
    expect(strip).not.toBeNull();
    expect(strip).toHaveClass("custom-class");
  });
});
