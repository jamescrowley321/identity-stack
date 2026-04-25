import * as React from "react"

import { cn } from "@/lib/utils"

type Trend = "up" | "down" | "neutral"

interface KpiItem {
  label: string
  value: string | number
  delta?: string
  trend?: Trend
}

const trendClass: Record<Trend, string> = {
  up: "text-success",
  down: "text-destructive",
  neutral: "text-muted-foreground",
}

function KpiStrip({
  items,
  className,
  ...props
}: React.ComponentProps<"div"> & { items: KpiItem[] }) {
  return (
    <div
      data-slot="kpi-strip"
      className={cn(
        "grid grid-cols-2 overflow-hidden rounded-xl border border-border bg-card md:grid-cols-[repeat(auto-fit,minmax(180px,1fr))]",
        className,
      )}
      {...props}
    >
      {items.map((item, i) => (
        <KpiItemCell key={i} item={item} isLast={i === items.length - 1} />
      ))}
    </div>
  )
}

function KpiItemCell({
  item,
  isLast,
}: {
  item: KpiItem
  isLast: boolean
}) {
  return (
    <div
      data-slot="kpi-item"
      className={cn(
        "px-5 py-4",
        !isLast && "border-r border-border max-md:[&:nth-child(2n)]:border-r-0",
      )}
    >
      <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
        {item.label}
      </div>
      <div className="mt-1.5 text-2xl font-semibold leading-tight tracking-tight tabular-nums">
        {item.value}
      </div>
      {item.delta && (
        <div
          data-slot="kpi-delta"
          className={cn(
            "mt-1 text-xs tabular-nums",
            trendClass[item.trend ?? "neutral"],
          )}
        >
          {item.delta}
        </div>
      )}
    </div>
  )
}

export { KpiStrip, type KpiItem, type Trend }
