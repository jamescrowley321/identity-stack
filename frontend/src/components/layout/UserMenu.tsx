import { useAuth } from "react-oidc-context"
import { useNavigate } from "react-router-dom"
import { useCallback } from "react"
import { LogOut, User } from "lucide-react"
import { useApiClient } from "@/hooks/useApiClient"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Button } from "@/components/ui/button"

export function UserMenu() {
  const auth = useAuth()
  const navigate = useNavigate()
  const { apiFetch } = useApiClient()

  const displayName = auth.user?.profile?.name || auth.user?.profile?.email || "User"
  const initials = displayName
    .split(/[\s@]/)
    .filter(Boolean)
    .slice(0, 2)
    .map((s) => s[0].toUpperCase())
    .join("")

  const handleLogout = useCallback(async () => {
    try {
      await apiFetch("/api/auth/logout", { method: "POST" })
    } catch {
      // Best-effort
    }
    await auth.removeUser()
    navigate("/login")
  }, [apiFetch, auth, navigate])

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="rounded-full">
          <Avatar size="sm">
            <AvatarFallback>{initials}</AvatarFallback>
          </Avatar>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <div className="px-2 py-1.5 text-sm">
          <p className="font-medium">{displayName}</p>
          {auth.user?.profile?.email && displayName !== auth.user.profile.email && (
            <p className="text-xs text-muted-foreground">{auth.user.profile.email}</p>
          )}
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => navigate("/profile")}>
          <User />
          Profile
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={handleLogout}>
          <LogOut />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
