import { Home, Users, Shield, Key, Settings, User, Lock } from "lucide-react"
import { useLocation, Link } from "react-router-dom"
import { useRBAC } from "@/hooks/useRBAC"
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
} from "@/components/ui/sidebar"

const navItems = [
  { to: "/", label: "Dashboard", icon: Home },
  { to: "/members", label: "Members", icon: Users, adminOnly: true },
  { to: "/roles", label: "Roles", icon: Shield, adminOnly: true },
  { to: "/keys", label: "Access Keys", icon: Key, adminOnly: true },
  { to: "/fga", label: "FGA", icon: Lock, adminOnly: true },
  { to: "/settings", label: "Tenant Settings", icon: Settings },
  { to: "/profile", label: "Profile", icon: User },
]

export function AppSidebar() {
  const location = useLocation()
  const { isAdmin } = useRBAC()

  const visibleItems = navItems.filter((item) => !item.adminOnly || isAdmin)

  return (
    <Sidebar>
      <SidebarHeader className="border-b px-4 py-3">
        <span className="text-lg font-semibold">Descope Starter</span>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Navigation</SidebarGroupLabel>
          <SidebarMenu>
            {visibleItems.map((item) => {
              const isActive =
                item.to === "/"
                  ? location.pathname === "/"
                  : location.pathname.startsWith(item.to)
              return (
                <SidebarMenuItem key={item.to}>
                  <SidebarMenuButton asChild isActive={isActive}>
                    <Link to={item.to}>
                      <item.icon />
                      <span>{item.label}</span>
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              )
            })}
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  )
}
