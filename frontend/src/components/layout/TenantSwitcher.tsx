import { useState } from "react"
import { useTenants, TenantInfo } from "@/hooks/useTenants"
import { useIdentityContext } from "@/contexts/IdentityContext"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

/**
 * Displays the user's current tenant and allows switching between tenants.
 *
 * Tenant names and memberships come from the canonical `GET /api/identity`
 * payload (via useTenants), so no separate `/api/tenants` fetch is needed.
 * How a switch happens is IdentityContext's `tenantSwitch`:
 *
 * - `client`: the tenant does not come from the token, so switching updates
 *   the client-side active-tenant selection; there is no token re-issue.
 * - `sign-in`: the tenant comes from the token, so switching signs in again
 *   for the chosen tenant (the configured tenant scope template).
 * - `none`: the tenant comes from the token and no template is configured, so
 *   the current tenant is shown and no switch is offered. The UI never shows
 *   one tenant while the API acts in another.
 *
 * A tenant sign-in whose token does not name the tenant asked for is reported
 * beside the current tenant and the switch (alone when there is no current
 * tenant), and the tenant asked for is not shown as current. A sign-in request that
 * fails before leaving the page is reported beside the switch, which keeps
 * showing the current tenant and can be used again; that newer report
 * replaces a failed switch report, so only one is shown.
 */
export default function TenantSwitcher() {
  const { currentTenantId, tenants } = useTenants()
  const {
    setCurrentTenantId,
    tenantSwitch = "client",
    signInToTenant,
    signInAgain,
    tenantSwitchFailure,
  } = useIdentityContext()
  const [requestFailedFor, setRequestFailedFor] = useState<string | null>(null)

  const displayName = (t: TenantInfo) => t.name || t.id

  const requestSignInAgain = () => {
    signInAgain().catch((error: unknown) => console.error("Sign-in request failed", error))
  }

  const handleSwitch = (tenantId: string) => {
    if (tenantId === currentTenantId) return
    if (tenantSwitch === "sign-in") {
      // A request that fails before the redirect leaves the user signed in
      // to the current tenant: say so beside the switch, which stays usable.
      setRequestFailedFor(null)
      signInToTenant(tenantId).catch((error: unknown) => {
        console.error("Tenant sign-in request failed", tenantId, error)
        setRequestFailedFor(tenantId)
      })
      return
    }
    setCurrentTenantId(tenantId)
  }

  const renderCurrent = () => {
    if (tenants.length === 0) {
      return <span className="text-sm text-muted-foreground">No tenants</span>
    }

    if (tenantSwitch === "none") {
      const shown = tenants.find((t) => t.id === currentTenantId)
      return <Badge variant="secondary">{shown ? displayName(shown) : currentTenantId}</Badge>
    }

    if (tenants.length === 1) {
      return <Badge variant="secondary">{displayName(tenants[0])}</Badge>
    }

    return (
      <Select value={currentTenantId ?? ""} onValueChange={handleSwitch}>
        <SelectTrigger className="h-7 w-auto gap-1 text-xs">
          <SelectValue placeholder="Select tenant..." />
        </SelectTrigger>
        <SelectContent>
          {tenants.map((t: TenantInfo) => (
            <SelectItem key={t.id} value={t.id}>
              {displayName(t)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  }

  const switchFailureAlert = tenantSwitchFailure && (
    <div role="alert" className="flex items-center gap-2 text-sm text-destructive">
      <span>Could not switch to {tenantSwitchFailure.requestedTenantId}</span>
      <Button variant="outline" size="sm" className="h-7" onClick={requestSignInAgain}>
        Sign in again
      </Button>
    </div>
  )

  // With no current tenant there is nothing to show beside the report.
  if (switchFailureAlert && currentTenantId === null) return switchFailureAlert

  const current = renderCurrent()
  // One report at a time: a failed sign-in request is newer than the switch
  // failure the page loaded with, so it replaces that report.
  const report =
    requestFailedFor !== null ? (
      <span role="alert" className="text-sm text-destructive">
        Could not switch to {requestFailedFor}
      </span>
    ) : (
      switchFailureAlert
    )
  if (!report) return current

  return (
    <div className="flex items-center gap-2">
      {current}
      {report}
    </div>
  )
}
