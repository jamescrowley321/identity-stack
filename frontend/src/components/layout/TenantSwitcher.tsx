import { useAuth } from "react-oidc-context"
import { useTenants, TenantInfo } from "@/hooks/useTenants"
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
 * Switching tenants triggers a new sign-in with the `tenant` parameter,
 * which tells Descope to issue tokens scoped to the selected tenant.
 */
export default function TenantSwitcher() {
  const auth = useAuth()
  const { currentTenantId, tenants } = useTenants()

  if (tenants.length === 0) {
    return <span className="text-sm text-muted-foreground">No tenants</span>
  }

  const handleSwitch = (tenantId: string) => {
    if (tenantId === currentTenantId) return
    auth.signinRedirect({ extraQueryParams: { tenant: tenantId } })
  }

  if (tenants.length === 1) {
    return <Badge variant="secondary">{tenants[0].id}</Badge>
  }

  return (
    <Select value={currentTenantId ?? ""} onValueChange={handleSwitch}>
      <SelectTrigger className="h-7 w-auto gap-1 text-xs">
        <SelectValue placeholder="Select tenant..." />
      </SelectTrigger>
      <SelectContent>
        {tenants.map((t: TenantInfo) => (
          <SelectItem key={t.id} value={t.id}>
            {t.id}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
