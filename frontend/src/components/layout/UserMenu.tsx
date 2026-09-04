import { useAuth } from "react-oidc-context"
import { useNavigate } from "react-router-dom"
import { useCallback, useState } from "react"
import { LogOut, User } from "lucide-react"
import { toast } from "sonner"
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

  const [isLoggingOut, setIsLoggingOut] = useState(false)

  const handleLogout = useCallback(async () => {
    if (isLoggingOut) return
    setIsLoggingOut(true)
    // Provider-aware logout: the backend returns a `logout_url` for RP-initiated
    // providers (Ory) that we must redirect the browser to; Descope revokes
    // server-side and returns none, so we just navigate to /login.
    let logoutUrl: string | undefined
    let backendRejected = false
    try {
      const response = await apiFetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id_token: auth.user?.id_token }),
      })
      if (response.ok) {
        const data = await response.json().catch(() => null)
        logoutUrl = data?.logout_url
      } else {
        // The backend failed closed (e.g. an RP-initiated provider advertises no
        // end_session_endpoint). We MUST NOT report success or clear the local
        // session — the provider (OP) session may still be live, and navigating
        // to /login would silently strand it. Surface the error and let the user
        // retry.
        backendRejected = true
      }
    } catch {
      // Network failure (no structured response) — fall through to a best-effort
      // local clear so the user is not stuck in a broken session.
    }
    if (backendRejected) {
      toast.error("Sign out failed. Please try again.")
      setIsLoggingOut(false)
      return
    }
    try {
      await auth.removeUser()
    } catch {
      // removeUser failed — still redirect/navigate away.
    }
    if (logoutUrl) {
      window.location.assign(logoutUrl)
      return
    }
    navigate("/login")
  }, [apiFetch, auth, navigate, isLoggingOut])

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
        <DropdownMenuItem onClick={handleLogout} disabled={isLoggingOut}>
          <LogOut />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
