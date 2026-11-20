import { useTenants, TenantInfo } from "@/hooks/useTenants"
import { useIdentityContext } from "@/contexts/IdentityContext"
import { Badge } from "@/components/ui/badge"
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
 * Switching updates the client-side active-tenant selection — the canonical
 * resolution is provider-neutral, so there is no token re-issue.
 */
export default function TenantSwitcher() {
  const { currentTenantId, tenants } = useTenants()
  const { setCurrentTenantId } = useIdentityContext()

  const displayName = (t: TenantInfo) => t.name || t.id

  if (tenants.length === 0) {
    return <span className="text-sm text-muted-foreground">No tenants</span>
  }

  const handleSwitch = (tenantId: string) => {
    if (tenantId === currentTenantId) return
    setCurrentTenantId(tenantId)
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
