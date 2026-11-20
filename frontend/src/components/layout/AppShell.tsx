import { Outlet } from "react-router-dom"
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar"
import { AppSidebar } from "@/components/layout/AppSidebar"
import { Header } from "@/components/layout/Header"
import { Toaster } from "@/components/ui/sonner"
import { useIdentityContext } from "@/contexts/IdentityContext"
import { CapabilitiesProvider, useCapabilities } from "@/contexts/CapabilitiesContext"

export function AppShell() {
  return (
    <CapabilitiesProvider>
      <Shell />
    </CapabilitiesProvider>
  )
}

function Shell() {
  // A page loads its data on mount, in the tenant the API client names. Until
  // the identity answers, that tenant is not known, so the page waits: a call
  // without it is refused for a caller in several tenants.
  //
  // A tenant switch mounts the page afresh: nothing it held for the previous
  // tenant (rows, a new key's secret, a half-typed form) survives, and a call
  // that was still running there can no longer change what is shown.
  //
  // The deployment's capabilities decide which pages there are, so a page
  // linked straight to waits for them too.
  const { loading: identityLoading, currentTenantId } = useIdentityContext()
  const { loading: capabilitiesLoading } = useCapabilities()
  const loading = identityLoading || capabilitiesLoading

  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
        <Header />
        <main className="flex-1 overflow-auto">
          {loading ? <p className="p-6 text-muted-foreground">Loading...</p> : <Outlet key={currentTenantId ?? ""} />}
        </main>
      </SidebarInset>
      <Toaster />
    </SidebarProvider>
  )
}
