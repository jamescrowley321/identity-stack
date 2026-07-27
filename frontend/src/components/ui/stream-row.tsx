import * as React from "react"

import { cn } from "@/lib/utils"

type StreamVerb = "create" | "update" | "delete" | "skip"

interface StreamEvent {
  timestamp: string
  icon?: React.ReactNode
  verb: StreamVerb
  subject: string
  code?: string
}

const verbClass: Record<StreamVerb, string> = {
  create: "text-success",
  update: "text-[oklch(0.45_0.18_235)]",
  delete: "text-destructive",
  skip: "text-muted-foreground",
}

function StreamRow({
  event,
  className,
  ...props
}: React.ComponentProps<"div"> & { event: StreamEvent }) {
  return (
    <div
      {...props}
      data-slot="stream-row"
      data-verb={event.verb}
      className={cn(
        "grid grid-cols-[132px_28px_1fr_auto] items-center gap-3 border-b border-border px-3.5 py-2.5 font-mono text-[13.5px]",
        className,
      )}
    >
      <span
        data-slot="stream-ts"
        className="text-xs tabular-nums text-muted-foreground"
      >
        {event.timestamp}
      </span>
      <span
        data-slot="stream-icon"
        className="flex items-center justify-center text-muted-foreground"
      >
        {event.icon}
      </span>
      <span data-slot="stream-body" className="truncate">
        <span className={cn("font-semibold", verbClass[event.verb])}>
          {event.verb}
        </span>{" "}
        <span className="text-foreground">{event.subject}</span>
      </span>
      {event.code && (
        <span
          data-slot="stream-code"
          className="text-xs tabular-nums text-muted-foreground"
        >
          {event.code}
        </span>
      )}
    </div>
  )
}

export { StreamRow, type StreamEvent, type StreamVerb }
