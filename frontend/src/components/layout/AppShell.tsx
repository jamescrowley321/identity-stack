import { Outlet } from "react-router-dom"
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar"
import { AppSidebar } from "@/components/layout/AppSidebar"
import { Header } from "@/components/layout/Header"
import { Toaster } from "@/components/ui/sonner"
import { useIdentityContext } from "@/contexts/IdentityContext"

export function AppShell() {
  // A page loads its data on mount, in the tenant the API client names. Until
  // the identity answers, that tenant is not known, so the page waits: a call
  // without it is refused for a caller in several tenants.
  const { loading } = useIdentityContext()

  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
        <Header />
        <main className="flex-1 overflow-auto">
          {loading ? <p className="p-6 text-muted-foreground">Loading...</p> : <Outlet />}
        </main>
      </SidebarInset>
      <Toaster />
    </SidebarProvider>
  )
}
