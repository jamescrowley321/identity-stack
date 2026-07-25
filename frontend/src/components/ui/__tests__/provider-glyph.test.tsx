import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ProviderGlyph, type ProviderType } from "../provider-glyph";

const allProviders: ProviderType[] = [
  "descope",
  "okta",
  "auth0",
  "entra",
  "cognito",
  "google",
  "ory",
  "generic",
];

describe("ProviderGlyph", () => {
  it("renders correct abbreviation for each provider", () => {
    const expected: Record<ProviderType, string> = {
      descope: "DSC",
      okta: "OKT",
      auth0: "A0",
      entra: "ENT",
      cognito: "COG",
      google: "GOO",
      ory: "ORY",
      generic: "GEN",
    };

    for (const provider of allProviders) {
      const { unmount } = render(<ProviderGlyph provider={provider} />);
      expect(screen.getByText(expected[provider])).toBeInTheDocument();
      unmount();
    }
  });

  it("applies data-slot and data-provider attributes", () => {
    const { container } = render(<ProviderGlyph provider="okta" />);
    const el = container.querySelector('[data-slot="provider-glyph"]');
    expect(el).not.toBeNull();
    expect(el).toHaveAttribute("data-provider", "okta");
  });

  it("renders all 8 variants without error", () => {
    const { container } = render(
      <>
        {allProviders.map((p) => (
          <ProviderGlyph key={p} provider={p} />
        ))}
      </>
    );
    expect(
      container.querySelectorAll('[data-slot="provider-glyph"]')
    ).toHaveLength(8);
  });

  it("applies default size classes", () => {
    const { container } = render(<ProviderGlyph provider="descope" />);
    const el = container.querySelector('[data-slot="provider-glyph"]');
    expect(el).not.toBeNull();
    expect(el).toHaveClass("size-9");
  });

  it("applies sm size classes", () => {
    const { container } = render(<ProviderGlyph provider="descope" size="sm" />);
    const el = container.querySelector('[data-slot="provider-glyph"]');
    expect(el).not.toBeNull();
    expect(el).toHaveClass("size-6");
  });

  it("applies lg size classes", () => {
    const { container } = render(<ProviderGlyph provider="descope" size="lg" />);
    const el = container.querySelector('[data-slot="provider-glyph"]');
    expect(el).not.toBeNull();
    expect(el).toHaveClass("size-12");
  });

  it("defaults to generic when provider is omitted", () => {
    const { container } = render(<ProviderGlyph />);
    const el = container.querySelector('[data-slot="provider-glyph"]');
    expect(el).not.toBeNull();
    expect(el).toHaveAttribute("data-provider", "generic");
    expect(el).toHaveTextContent("GEN");
  });

  it("applies a distinct background color for each provider", () => {
    const bgByProvider = new Map<ProviderType, string>();
    for (const provider of allProviders) {
      const { container, unmount } = render(
        <ProviderGlyph provider={provider} />
      );
      const el = container.querySelector('[data-slot="provider-glyph"]');
      expect(el).not.toBeNull();
      const bg = Array.from(el!.classList).find((c) => c.startsWith("bg-"));
      expect(bg, `provider "${provider}" must have a bg-* class`).toBeDefined();
      bgByProvider.set(provider, bg!);
      unmount();
    }
    const uniqueBackgrounds = new Set(bgByProvider.values());
    expect(uniqueBackgrounds.size).toBe(allProviders.length);
  });

  it("merges custom className", () => {
    const { container } = render(
      <ProviderGlyph provider="auth0" className="custom-class" />
    );
    const el = container.querySelector('[data-slot="provider-glyph"]');
    expect(el).not.toBeNull();
    expect(el).toHaveClass("custom-class");
  });
});
