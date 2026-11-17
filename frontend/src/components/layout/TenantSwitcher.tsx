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
 * A tenant sign-in whose token does not name the tenant asked for is reported,
 * and the tenant asked for is not shown as current. A sign-in request that
 * fails before leaving the page is reported beside the switch, which keeps
 * showing the current tenant and can be used again.
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

  if (tenantSwitchFailure) {
    return (
      <div role="alert" className="flex items-center gap-2 text-sm text-destructive">
        <span>Could not switch to {tenantSwitchFailure.requestedTenantId}</span>
        <Button variant="outline" size="sm" className="h-7" onClick={requestSignInAgain}>
          Sign in again
        </Button>
      </div>
    )
  }

  if (tenants.length === 0) {
    return <span className="text-sm text-muted-foreground">No tenants</span>
  }

  const handleSwitch = (tenantId: string) => {
    if (tenantId === currentTenantId) return
    if (tenantSwitch === "sign-in") {
      // A request that fails before the redirect leaves the user signed in
      // to the current tenant: say so beside the switch, which stays usable.
      setRequestFailedFor(null)
      signInToTenant(tenantId).catch((error: unknown) => {
        console.error(`Tenant sign-in request for ${tenantId} failed`, error)
        setRequestFailedFor(tenantId)
      })
      return
    }
    setCurrentTenantId(tenantId)
  }

  if (tenantSwitch === "none") {
    const current = tenants.find((t) => t.id === currentTenantId)
    return <Badge variant="secondary">{current ? displayName(current) : currentTenantId}</Badge>
  }

  if (tenants.length === 1) {
    return <Badge variant="secondary">{displayName(tenants[0])}</Badge>
  }

  const switcher = (
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

  if (requestFailedFor === null) return switcher

  return (
    <div className="flex items-center gap-2">
      {switcher}
      <span role="alert" className="text-sm text-destructive">
        Could not switch to {requestFailedFor}
      </span>
    </div>
  )
}
