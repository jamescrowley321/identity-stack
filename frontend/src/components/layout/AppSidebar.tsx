import {
  Home,
  Users,
  Shield,
  Key,
  Settings,
  User,
  Lock,
  Globe,
  RefreshCw,
  Activity,
  UserPlus,
} from "lucide-react"
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

interface NavItem {
  to: string
  label: string
  icon: React.ComponentType
  adminOnly?: boolean
}

interface NavSection {
  label: string
  items: NavItem[]
}

const navSections: NavSection[] = [
  {
    label: "Workspace",
    items: [
      { to: "/", label: "Dashboard", icon: Home },
      { to: "/members", label: "Members", icon: Users, adminOnly: true },
      { to: "/roles", label: "Roles", icon: Shield, adminOnly: true },
      { to: "/keys", label: "Access Keys", icon: Key, adminOnly: true },
      { to: "/fga", label: "FGA", icon: Lock, adminOnly: true },
    ],
  },
  {
    label: "Platform",
    items: [
      { to: "/providers", label: "Providers", icon: Globe, adminOnly: true },
      {
        to: "/sync",
        label: "Sync Dashboard",
        icon: RefreshCw,
        adminOnly: true,
      },
      { to: "/events", label: "Events", icon: Activity, adminOnly: true },
      {
        to: "/provisional",
        label: "Provisional Users",
        icon: UserPlus,
        adminOnly: true,
      },
    ],
  },
  {
    label: "Tenant",
    items: [
      { to: "/settings", label: "Tenant Settings", icon: Settings },
      { to: "/profile", label: "Profile", icon: User },
    ],
  },
]

export function AppSidebar() {
  const location = useLocation()
  const { isAdmin } = useRBAC()

  return (
    <Sidebar>
      <SidebarHeader className="border-b px-4 py-3">
        <span className="text-lg font-semibold">Descope Starter</span>
      </SidebarHeader>
      <SidebarContent>
        {navSections.map((section) => {
          const visibleItems = section.items.filter(
            (item) => !item.adminOnly || isAdmin
          )
          if (visibleItems.length === 0) return null
          return (
            <SidebarGroup key={section.label}>
              <SidebarGroupLabel>{section.label}</SidebarGroupLabel>
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
          )
        })}
      </SidebarContent>
    </Sidebar>
  )
}
