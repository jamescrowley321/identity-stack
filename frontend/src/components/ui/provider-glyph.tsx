import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const providerGlyphVariants = cva(
  "inline-flex shrink-0 items-center justify-center rounded-lg border font-semibold leading-none",
  {
    variants: {
      provider: {
        descope:
          "border-[oklch(0.85_0.10_276)] bg-[oklch(0.96_0.05_276)] text-[oklch(0.40_0.20_276)]",
        okta:
          "border-[oklch(0.85_0.10_230)] bg-[oklch(0.97_0.04_230)] text-[oklch(0.42_0.18_230)]",
        auth0:
          "border-[oklch(0.85_0.10_30)] bg-[oklch(0.96_0.05_30)] text-[oklch(0.45_0.18_30)]",
        entra:
          "border-[oklch(0.85_0.10_215)] bg-[oklch(0.96_0.05_215)] text-[oklch(0.40_0.20_215)]",
        cognito:
          "border-[oklch(0.85_0.10_65)] bg-[oklch(0.97_0.05_65)] text-[oklch(0.42_0.18_65)]",
        google:
          "border-[oklch(0.86_0.08_130)] bg-[oklch(0.97_0.04_130)] text-[oklch(0.45_0.18_130)]",
        ory:
          "border-[oklch(0.86_0.10_340)] bg-[oklch(0.97_0.05_340)] text-[oklch(0.42_0.20_340)]",
        generic:
          "border-border bg-muted text-muted-foreground",
      },
      size: {
        default: "size-9 text-xs",
        sm: "size-6 rounded-md text-[10px]",
        lg: "size-12 rounded-[10px] text-base",
      },
    },
    defaultVariants: {
      provider: "generic",
      size: "default",
    },
  },
)

type ProviderType = NonNullable<
  VariantProps<typeof providerGlyphVariants>["provider"]
>

// Keyed on ProviderType so adding a provider variant without an abbreviation
// is a compile error — the abbreviation map and the cva variants stay in sync.
const providerAbbreviations: Record<ProviderType, string> = {
  descope: "DSC",
  okta: "OKT",
  auth0: "A0",
  entra: "ENT",
  cognito: "COG",
  google: "GOO",
  ory: "ORY",
  generic: "GEN",
}

function ProviderGlyph({
  provider = "generic",
  size,
  className,
  ...props
}: Omit<React.ComponentProps<"span">, "children"> &
  VariantProps<typeof providerGlyphVariants> & {
    provider?: ProviderType
  }) {
  // Fallback derives from the map (no hardcoded literal) so it never drifts.
  const abbreviation =
    providerAbbreviations[provider] ?? providerAbbreviations.generic

  return (
    <span
      {...props}
      data-slot="provider-glyph"
      data-provider={provider}
      className={cn(providerGlyphVariants({ provider, size }), className)}
    >
      {abbreviation}
    </span>
  )
}

export { ProviderGlyph, providerGlyphVariants, type ProviderType }
