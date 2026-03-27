import { useLocation } from "react-router-dom"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { Separator } from "@/components/ui/separator"
import TenantSwitcher from "@/components/layout/TenantSwitcher"
import { ThemeToggle } from "@/components/layout/ThemeToggle"
import { UserMenu } from "@/components/layout/UserMenu"

const routeLabels: Record<string, string> = {
  "/": "Dashboard",
  "/members": "Members",
  "/roles": "Roles",
  "/keys": "Access Keys",
  "/settings": "Tenant Settings",
  "/profile": "Profile",
}

export function Header() {
  const location = useLocation()
  const pageLabel = routeLabels[location.pathname] ?? "Page"

  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
      <SidebarTrigger className="-ml-1" />
      <Separator orientation="vertical" className="mr-2 h-4" />
      <span className="text-sm font-medium">{pageLabel}</span>
      <div className="ml-auto flex items-center gap-2">
        <TenantSwitcher />
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  )
}
