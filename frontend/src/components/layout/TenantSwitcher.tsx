import { useEffect, useState } from "react"
import { useAuth } from "react-oidc-context"
import { useTenants, TenantInfo } from "@/hooks/useTenants"
import { apiUrl } from "@/hooks/useApiClient"
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
 * Fetches tenant names from the backend API so the display shows
 * human-readable names instead of raw Descope tenant IDs.
 */
export default function TenantSwitcher() {
  const auth = useAuth()
  const { currentTenantId, tenants } = useTenants()
  const [tenantNames, setTenantNames] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!auth.user?.access_token || tenants.length === 0) return

    fetch(apiUrl("/api/tenants"), {
      headers: { Authorization: `Bearer ${auth.user.access_token}` },
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.tenants) {
          const names: Record<string, string> = {}
          for (const t of data.tenants) {
            names[t.id] = t.name || t.id
          }
          setTenantNames(names)
        }
      })
      .catch(() => {})
  }, [auth.user?.access_token, tenants.length])

  const displayName = (t: TenantInfo) => tenantNames[t.id] || t.id

  if (tenants.length === 0) {
    return <span className="text-sm text-muted-foreground">No tenants</span>
  }

  const handleSwitch = (tenantId: string) => {
    if (tenantId === currentTenantId) return
    auth.signinRedirect({ extraQueryParams: { tenant: tenantId } })
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
