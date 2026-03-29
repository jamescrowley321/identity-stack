import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { useApiClient } from "@/hooks/useApiClient";
import { useRBAC } from "@/hooks/useRBAC";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface RoleDefinition {
  name: string;
  description?: string;
  permissionNames?: string[];
}

interface PermissionDefinition {
  name: string;
  description?: string;
}

export default function RoleManagement() {
  const { apiFetch } = useApiClient();
  const { roles, permissions, isAdmin, currentTenantId } = useRBAC();

  // User role assignment state
  const [userId, setUserId] = useState("");
  const [selectedRole, setSelectedRole] = useState("");

  // Your roles state
  const [myRoles, setMyRoles] = useState<{ roles: string[]; permissions: string[] } | null>(null);

  // Role definitions state
  const [roleDefinitions, setRoleDefinitions] = useState<RoleDefinition[]>([]);
  const [rolesLoading, setRolesLoading] = useState(true);

  // Permission definitions state
  const [permissionDefinitions, setPermissionDefinitions] = useState<PermissionDefinition[]>([]);
  const [permissionsLoading, setPermissionsLoading] = useState(true);

  // Role dialog state
  const [roleDialogOpen, setRoleDialogOpen] = useState(false);
  const [editingRole, setEditingRole] = useState<RoleDefinition | null>(null);
  const [roleName, setRoleName] = useState("");
  const [roleDescription, setRoleDescription] = useState("");
  const [rolePermissions, setRolePermissions] = useState<string[]>([]);

  // Permission dialog state
  const [permDialogOpen, setPermDialogOpen] = useState(false);
  const [editingPerm, setEditingPerm] = useState<PermissionDefinition | null>(null);
  const [permName, setPermName] = useState("");
  const [permDescription, setPermDescription] = useState("");

  // Delete confirmation state
  const [deleteTarget, setDeleteTarget] = useState<{ type: "role" | "permission"; name: string } | null>(null);

  // Load roles and permissions
  useEffect(() => {
    apiFetch("/api/roles/me")
      .then((res) => (res.ok ? res.json() : null))
      .then(setMyRoles)
      .catch(() => {});
  }, [apiFetch]);

  const loadRoles = useCallback(() => {
    setRolesLoading(true);
    apiFetch("/api/roles")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.roles) setRoleDefinitions(data.roles);
      })
      .catch(() => {})
      .finally(() => setRolesLoading(false));
  }, [apiFetch]);

  const loadPermissions = useCallback(() => {
    setPermissionsLoading(true);
    apiFetch("/api/permissions")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.permissions) setPermissionDefinitions(data.permissions);
      })
      .catch(() => {})
      .finally(() => setPermissionsLoading(false));
  }, [apiFetch]);

  useEffect(() => {
    if (isAdmin) {
      loadRoles();
      loadPermissions();
    }
  }, [isAdmin, loadRoles, loadPermissions]);

  // Set default selected role when role definitions load
  useEffect(() => {
    if (roleDefinitions.length > 0 && !selectedRole) {
      setSelectedRole(roleDefinitions[0].name);
    }
  }, [roleDefinitions, selectedRole]);

  // --- Role assignment handlers ---

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

  // --- Role CRUD handlers ---

  const openCreateRole = useCallback(() => {
    setEditingRole(null);
    setRoleName("");
    setRoleDescription("");
    setRolePermissions([]);
    setRoleDialogOpen(true);
  }, []);

  const openEditRole = useCallback((role: RoleDefinition) => {
    setEditingRole(role);
    setRoleName(role.name);
    setRoleDescription(role.description || "");
    setRolePermissions(role.permissionNames || []);
    setRoleDialogOpen(true);
  }, []);

  const handleSaveRole = useCallback(async () => {
    if (!roleName.trim()) return;
    try {
      if (editingRole) {
        const body: Record<string, unknown> = {};
        if (roleName.trim() !== editingRole.name) body.new_name = roleName.trim();
        body.description = roleDescription;
        body.permission_names = rolePermissions;
        const res = await apiFetch(`/api/roles/${encodeURIComponent(editingRole.name)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (res.ok) {
          toast.success(`Role "${roleName.trim()}" updated`);
          setRoleDialogOpen(false);
          loadRoles();
        } else {
          const err = await res.json();
          toast.error(err.detail || "Failed to update role");
        }
      } else {
        const res = await apiFetch("/api/roles", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: roleName.trim(),
            description: roleDescription,
            permission_names: rolePermissions,
          }),
        });
        if (res.ok) {
          toast.success(`Role "${roleName.trim()}" created`);
          setRoleDialogOpen(false);
          loadRoles();
        } else {
          const err = await res.json();
          toast.error(err.detail || "Failed to create role");
        }
      }
    } catch {
      toast.error(editingRole ? "Failed to update role" : "Failed to create role");
    }
  }, [editingRole, roleName, roleDescription, rolePermissions, apiFetch, loadRoles]);

  const handleDeleteRole = useCallback(
    async (name: string) => {
      try {
        const res = await apiFetch(`/api/roles/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (res.ok) {
          toast.success(`Role "${name}" deleted`);
          loadRoles();
        } else {
          const err = await res.json();
          toast.error(err.detail || "Failed to delete role");
        }
      } catch {
        toast.error("Failed to delete role");
      }
    },
    [apiFetch, loadRoles],
  );

  // --- Permission CRUD handlers ---

  const openCreatePerm = useCallback(() => {
    setEditingPerm(null);
    setPermName("");
    setPermDescription("");
    setPermDialogOpen(true);
  }, []);

  const openEditPerm = useCallback((perm: PermissionDefinition) => {
    setEditingPerm(perm);
    setPermName(perm.name);
    setPermDescription(perm.description || "");
    setPermDialogOpen(true);
  }, []);

  const handleSavePerm = useCallback(async () => {
    if (!permName.trim()) return;
    try {
      if (editingPerm) {
        const res = await apiFetch(`/api/permissions/${encodeURIComponent(editingPerm.name)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ new_name: permName.trim(), description: permDescription }),
        });
        if (res.ok) {
          toast.success(`Permission "${permName.trim()}" updated`);
          setPermDialogOpen(false);
          loadPermissions();
          loadRoles(); // Roles may reference this permission
        } else {
          const err = await res.json();
          toast.error(err.detail || "Failed to update permission");
        }
      } else {
        const res = await apiFetch("/api/permissions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: permName.trim(), description: permDescription }),
        });
        if (res.ok) {
          toast.success(`Permission "${permName.trim()}" created`);
          setPermDialogOpen(false);
          loadPermissions();
        } else {
          const err = await res.json();
          toast.error(err.detail || "Failed to create permission");
        }
      }
    } catch {
      toast.error(editingPerm ? "Failed to update permission" : "Failed to create permission");
    }
  }, [editingPerm, permName, permDescription, apiFetch, loadPermissions, loadRoles]);

  const handleDeletePerm = useCallback(
    async (name: string) => {
      try {
        const res = await apiFetch(`/api/permissions/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (res.ok) {
          toast.success(`Permission "${name}" deleted`);
          loadPermissions();
          loadRoles(); // Roles may reference this permission
        } else {
          const err = await res.json();
          toast.error(err.detail || "Failed to delete permission");
        }
      } catch {
        toast.error("Failed to delete permission");
      }
    },
    [apiFetch, loadPermissions, loadRoles],
  );

  // --- Delete confirmation handler ---

  const handleConfirmDelete = useCallback(async () => {
    if (!deleteTarget) return;
    if (deleteTarget.type === "role") {
      await handleDeleteRole(deleteTarget.name);
    } else {
      await handleDeletePerm(deleteTarget.name);
    }
    setDeleteTarget(null);
  }, [deleteTarget, handleDeleteRole, handleDeletePerm]);

  // --- Permission checkbox toggle ---

  const togglePermission = useCallback(
    (permName: string) => {
      setRolePermissions((prev) =>
        prev.includes(permName) ? prev.filter((p) => p !== permName) : [...prev, permName],
      );
    },
    [],
  );

  return (
    <>
      <PageHeader title="Role Management" description="View and manage roles and permissions" />
      <div className="p-6 space-y-6">
        {/* Your Roles Card */}
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

        {/* Admin sections */}
        {isAdmin && (
          <>
            {/* Role Definitions */}
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle>Role Definitions</CardTitle>
                    <CardDescription>
                      {rolesLoading ? "Loading..." : `${roleDefinitions.length} role${roleDefinitions.length !== 1 ? "s" : ""}`}
                    </CardDescription>
                  </div>
                  <Button onClick={openCreateRole}>Create Role</Button>
                </div>
              </CardHeader>
              <CardContent>
                {rolesLoading ? (
                  <div className="space-y-2">
                    <Skeleton className="h-8 w-full" />
                    <Skeleton className="h-8 w-full" />
                    <Skeleton className="h-8 w-full" />
                  </div>
                ) : roleDefinitions.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No roles defined yet.</p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Name</TableHead>
                        <TableHead>Description</TableHead>
                        <TableHead>Permissions</TableHead>
                        <TableHead className="text-right">Actions</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {roleDefinitions.map((role) => (
                        <TableRow key={role.name}>
                          <TableCell className="font-medium">{role.name}</TableCell>
                          <TableCell className="text-muted-foreground">{role.description || "—"}</TableCell>
                          <TableCell>
                            <div className="flex gap-1 flex-wrap">
                              {(role.permissionNames || []).length > 0
                                ? role.permissionNames!.map((p) => (
                                    <Badge key={p} variant="secondary">{p}</Badge>
                                  ))
                                : <span className="text-sm text-muted-foreground">None</span>}
                            </div>
                          </TableCell>
                          <TableCell className="text-right">
                            <div className="flex gap-1 justify-end">
                              <Button variant="ghost" size="sm" onClick={() => openEditRole(role)}>Edit</Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="text-destructive"
                                onClick={() => setDeleteTarget({ type: "role", name: role.name })}
                              >
                                Delete
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>

            {/* Permission Management */}
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle>Permission Management</CardTitle>
                    <CardDescription>
                      {permissionsLoading ? "Loading..." : `${permissionDefinitions.length} permission${permissionDefinitions.length !== 1 ? "s" : ""}`}
                    </CardDescription>
                  </div>
                  <Button onClick={openCreatePerm}>Create Permission</Button>
                </div>
              </CardHeader>
              <CardContent>
                {permissionsLoading ? (
                  <div className="space-y-2">
                    <Skeleton className="h-8 w-full" />
                    <Skeleton className="h-8 w-full" />
                    <Skeleton className="h-8 w-full" />
                  </div>
                ) : permissionDefinitions.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No permissions defined yet.</p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Name</TableHead>
                        <TableHead>Description</TableHead>
                        <TableHead className="text-right">Actions</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {permissionDefinitions.map((perm) => (
                        <TableRow key={perm.name}>
                          <TableCell className="font-medium">{perm.name}</TableCell>
                          <TableCell className="text-muted-foreground">{perm.description || "—"}</TableCell>
                          <TableCell className="text-right">
                            <div className="flex gap-1 justify-end">
                              <Button variant="ghost" size="sm" onClick={() => openEditPerm(perm)}>Edit</Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="text-destructive"
                                onClick={() => setDeleteTarget({ type: "permission", name: perm.name })}
                              >
                                Delete
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>

            {/* Manage User Roles (existing assignment UI) */}
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
                      {roleDefinitions.map((r) => (
                        <SelectItem key={r.name} value={r.name}>{r.name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Button onClick={handleAssign} disabled={!userId.trim() || !selectedRole}>Assign</Button>
                  <Button variant="outline" onClick={handleRemove} disabled={!userId.trim() || !selectedRole}>Remove</Button>
                </div>
              </CardContent>
            </Card>
          </>
        )}

        {!isAdmin && (
          <Alert>
            <AlertDescription>
              You need an admin or owner role to manage roles and permissions.
            </AlertDescription>
          </Alert>
        )}
      </div>

      {/* Role Create/Edit Dialog */}
      <Dialog open={roleDialogOpen} onOpenChange={setRoleDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{editingRole ? "Edit Role" : "Create Role"}</DialogTitle>
            <DialogDescription>
              {editingRole ? `Editing "${editingRole.name}"` : "Define a new role with optional permissions."}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="role-name">Name</Label>
              <Input
                id="role-name"
                placeholder="Role name"
                value={roleName}
                onChange={(e) => setRoleName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="role-desc">Description</Label>
              <Input
                id="role-desc"
                placeholder="Role description"
                value={roleDescription}
                onChange={(e) => setRoleDescription(e.target.value)}
              />
            </div>
            {permissionDefinitions.length > 0 && (
              <div className="space-y-2">
                <Label>Permissions</Label>
                <div className="space-y-1 max-h-40 overflow-y-auto border rounded p-2">
                  {permissionDefinitions.map((perm) => (
                    <label key={perm.name} className="flex items-center gap-2 text-sm cursor-pointer">
                      <input
                        type="checkbox"
                        checked={rolePermissions.includes(perm.name)}
                        onChange={() => togglePermission(perm.name)}
                      />
                      {perm.name}
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRoleDialogOpen(false)}>Cancel</Button>
            <Button onClick={handleSaveRole} disabled={!roleName.trim()}>
              {editingRole ? "Save Changes" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Permission Create/Edit Dialog */}
      <Dialog open={permDialogOpen} onOpenChange={setPermDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{editingPerm ? "Edit Permission" : "Create Permission"}</DialogTitle>
            <DialogDescription>
              {editingPerm ? `Editing "${editingPerm.name}"` : "Define a new permission."}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="perm-name">Name</Label>
              <Input
                id="perm-name"
                placeholder="Permission name"
                value={permName}
                onChange={(e) => setPermName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="perm-desc">Description</Label>
              <Input
                id="perm-desc"
                placeholder="Permission description"
                value={permDescription}
                onChange={(e) => setPermDescription(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPermDialogOpen(false)}>Cancel</Button>
            <Button onClick={handleSavePerm} disabled={!permName.trim()}>
              {editingPerm ? "Save Changes" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteTarget !== null} onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm Delete</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete the {deleteTarget?.type} &quot;{deleteTarget?.name}&quot;? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>Cancel</Button>
            <Button variant="destructive" onClick={handleConfirmDelete}>Delete</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
