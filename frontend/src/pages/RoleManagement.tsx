import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { useApiClient } from "@/hooks/useApiClient";
import { useRBAC } from "@/hooks/useRBAC";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const AVAILABLE_ROLES = ["owner", "admin", "member", "viewer"];

export default function RoleManagement() {
  const { apiFetch } = useApiClient();
  const { roles, permissions, isAdmin, currentTenantId } = useRBAC();
  const [userId, setUserId] = useState("");
  const [selectedRole, setSelectedRole] = useState(AVAILABLE_ROLES[2]);

  const [myRoles, setMyRoles] = useState<{ roles: string[]; permissions: string[] } | null>(null);

  useEffect(() => {
    apiFetch("/api/roles/me")
      .then((res) => (res.ok ? res.json() : null))
      .then(setMyRoles)
      .catch(() => {});
  }, [apiFetch]);

  const handleAssign = useCallback(async () => {
    if (!userId.trim() || !currentTenantId) return;
    try {
      const res = await apiFetch("/api/roles/assign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId.trim(), tenant_id: currentTenantId, role_names: [selectedRole] }),
      });
      if (res.ok) {
        toast.success(`Assigned "${selectedRole}" to ${userId.trim()}`);
        setUserId("");
      } else {
        const err = await res.json();
        toast.error(err.detail || res.statusText);
      }
    } catch {
      toast.error("Failed to assign role");
    }
  }, [userId, selectedRole, currentTenantId, apiFetch]);

  const handleRemove = useCallback(async () => {
    if (!userId.trim() || !currentTenantId) return;
    try {
      const res = await apiFetch("/api/roles/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId.trim(), tenant_id: currentTenantId, role_names: [selectedRole] }),
      });
      if (res.ok) {
        toast.success(`Removed "${selectedRole}" from ${userId.trim()}`);
        setUserId("");
      } else {
        const err = await res.json();
        toast.error(err.detail || res.statusText);
      }
    } catch {
      toast.error("Failed to remove role");
    }
  }, [userId, selectedRole, currentTenantId, apiFetch]);

  return (
    <>
      <PageHeader title="Role Management" description="View and manage user roles" />
      <div className="p-6 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Your Roles</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Roles:</span>
              <div className="flex gap-1">
                {roles.length > 0 ? roles.map((r) => (
                  <Badge key={r} variant="outline">{r}</Badge>
                )) : <span className="text-sm text-muted-foreground">None</span>}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Permissions:</span>
              <div className="flex gap-1 flex-wrap">
                {permissions.length > 0 ? permissions.map((p) => (
                  <Badge key={p} variant="secondary">{p}</Badge>
                )) : <span className="text-sm text-muted-foreground">None</span>}
              </div>
            </div>
            {myRoles && (
              <p className="text-xs text-muted-foreground">
                Server-confirmed: {myRoles.roles.join(", ") || "none"}
              </p>
            )}
          </CardContent>
        </Card>

        {isAdmin && (
          <Card>
            <CardHeader>
              <CardTitle>Manage User Roles</CardTitle>
              <CardDescription>
                Assign or remove roles for users in tenant {currentTenantId}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex gap-2 flex-wrap">
                <Input
                  placeholder="User ID (login ID)"
                  value={userId}
                  onChange={(e) => setUserId(e.target.value)}
                  className="max-w-xs"
                />
                <Select value={selectedRole} onValueChange={setSelectedRole}>
                  <SelectTrigger className="w-32">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {AVAILABLE_ROLES.map((r) => (
                      <SelectItem key={r} value={r}>{r}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button onClick={handleAssign} disabled={!userId.trim()}>Assign</Button>
                <Button variant="outline" onClick={handleRemove} disabled={!userId.trim()}>Remove</Button>
              </div>
            </CardContent>
          </Card>
        )}

        {!isAdmin && (
          <Alert>
            <AlertDescription>
              You need an admin or owner role to manage other users' roles.
            </AlertDescription>
          </Alert>
        )}
      </div>
    </>
  );
}
