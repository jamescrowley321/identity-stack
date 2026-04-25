import * as React from "react"

const MOBILE_BREAKPOINT = 768
const TABLET_BREAKPOINT = 1024

export type Breakpoint = "mobile" | "tablet" | "desktop"

export function useBreakpoint(): Breakpoint {
  const [breakpoint, setBreakpoint] = React.useState<Breakpoint>(() => {
    const w = window.innerWidth
    if (w < MOBILE_BREAKPOINT) return "mobile"
    if (w < TABLET_BREAKPOINT) return "tablet"
    return "desktop"
  })

  React.useEffect(() => {
    const mobileQuery = window.matchMedia(
      `(max-width: ${MOBILE_BREAKPOINT - 1}px)`
    )
    const tabletQuery = window.matchMedia(
      `(min-width: ${MOBILE_BREAKPOINT}px) and (max-width: ${TABLET_BREAKPOINT - 1}px)`
    )

    const update = () => {
      if (mobileQuery.matches) {
        setBreakpoint("mobile")
      } else if (tabletQuery.matches) {
        setBreakpoint("tablet")
      } else {
        setBreakpoint("desktop")
      }
    }

    mobileQuery.addEventListener("change", update)
    tabletQuery.addEventListener("change", update)
    return () => {
      mobileQuery.removeEventListener("change", update)
      tabletQuery.removeEventListener("change", update)
    }
  }, [])

  return breakpoint
}

export function useIsMobile() {
  const breakpoint = useBreakpoint()
  return breakpoint === "mobile"
}
