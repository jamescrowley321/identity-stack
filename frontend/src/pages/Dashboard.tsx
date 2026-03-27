import { useAuth } from "react-oidc-context";
import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { useApiClient } from "@/hooks/useApiClient";
import { useTenants } from "@/hooks/useTenants";
import { useRBAC } from "@/hooks/useRBAC";
import { RequirePermission } from "@/components/auth/RequirePermission";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";

interface TenantResource {
  id: string;
  tenant_id: string;
  name: string;
  description: string;
  created_at: string;
}

export default function Dashboard() {
  const auth = useAuth();
  const { apiFetch } = useApiClient();
  const { currentTenantId, tenants } = useTenants();
  const { roles } = useRBAC();
  const [health, setHealth] = useState<string>("checking...");
  const [accessTokenClaims, setAccessTokenClaims] = useState<Record<string, unknown> | null>(null);
  const [idTokenClaims, setIdTokenClaims] = useState<Record<string, unknown> | null>(null);
  const [identity, setIdentity] = useState<Record<string, unknown> | null>(null);
  const [resources, setResources] = useState<TenantResource[]>([]);
  const [newResourceName, setNewResourceName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const idToken = auth.user?.id_token;

  useEffect(() => {
    fetch("/api/health")
      .then((res) => res.json())
      .then((data) => setHealth(data.status))
      .catch(() => setHealth("unreachable"));
  }, []);

  useEffect(() => {
    if (!auth.user?.access_token) return;

    apiFetch("/api/claims")
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then(setAccessTokenClaims)
      .catch((err) => setError(err.message));

    apiFetch("/api/me")
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then(setIdentity)
      .catch(() => {});
  }, [auth.user?.access_token, apiFetch]);

  useEffect(() => {
    if (!idToken || !auth.user?.access_token) return;

    fetch("/api/validate-id-token", {
      method: "POST",
      headers: { Authorization: `Bearer ${idToken}` },
    })
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then(setIdTokenClaims)
      .catch(() => {});
  }, [idToken, auth.user?.access_token]);

  const loadResources = useCallback(() => {
    if (!currentTenantId || !auth.user?.access_token) return;
    apiFetch(`/api/tenants/${currentTenantId}/resources`)
      .then((res) => {
        if (!res.ok) return;
        return res.json();
      })
      .then((data) => {
        if (data) setResources(data.resources);
      })
      .catch(() => {});
  }, [currentTenantId, auth.user?.access_token, apiFetch]);

  useEffect(() => {
    loadResources();
  }, [loadResources]);

  const handleCreateResource = useCallback(async () => {
    if (!currentTenantId || !newResourceName.trim()) return;
    try {
      const res = await apiFetch(`/api/tenants/${currentTenantId}/resources`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newResourceName.trim(), description: "" }),
      });
      if (res.ok) {
        toast.success(`Resource "${newResourceName.trim()}" created`);
        setNewResourceName("");
        loadResources();
      } else {
        const err = await res.json();
        toast.error(err.detail || "Failed to create resource");
      }
    } catch {
      toast.error("Failed to create resource");
    }
  }, [currentTenantId, newResourceName, apiFetch, loadResources]);

  const identityName = identity?.identity as Record<string, unknown> | undefined;
  const displayName = (identityName?.name as string) || auth.user?.profile?.email || "User";

  return (
    <>
      <PageHeader title={`Welcome, ${displayName}`} />
      <div className="p-6">
        {error && (
          <Alert variant="destructive" className="mb-6">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <Tabs defaultValue="overview">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            {currentTenantId && <TabsTrigger value="resources">Resources</TabsTrigger>}
            <TabsTrigger value="claims">Claims</TabsTrigger>
          </TabsList>

          <TabsContent value="overview" className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>Status</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-muted-foreground">Backend:</span>
                  <Badge variant={health === "ok" ? "secondary" : "destructive"}>
                    {health}
                  </Badge>
                </div>
                {currentTenantId && (
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground">Tenant:</span>
                    <Badge variant="outline">{currentTenantId}</Badge>
                  </div>
                )}
                {roles.length > 0 && (
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground">Roles:</span>
                    <div className="flex gap-1">
                      {roles.map((role) => (
                        <Badge key={role} variant="outline">{role}</Badge>
                      ))}
                    </div>
                  </div>
                )}
                {tenants.length > 0 && (
                  <div className="flex items-center gap-2">
                    <span className="text-sm text-muted-foreground">Memberships:</span>
                    <span className="text-sm">{tenants.map((t) => t.id).join(", ")}</span>
                  </div>
                )}
              </CardContent>
            </Card>
          </TabsContent>

          {currentTenantId && (
            <TabsContent value="resources" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle>Tenant Resources</CardTitle>
                  <CardDescription>Scoped to {currentTenantId}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                  <RequirePermission permission="documents.write">
                    <div className="flex gap-2">
                      <Input
                        placeholder="Resource name"
                        value={newResourceName}
                        onChange={(e) => setNewResourceName(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && handleCreateResource()}
                        className="max-w-sm"
                      />
                      <Button onClick={handleCreateResource} disabled={!newResourceName.trim()}>
                        Create
                      </Button>
                    </div>
                  </RequirePermission>
                  {resources.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No resources yet.</p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Name</TableHead>
                          <TableHead>ID</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {resources.map((r) => (
                          <TableRow key={r.id}>
                            <TableCell className="font-medium">{r.name}</TableCell>
                            <TableCell className="text-xs text-muted-foreground">{r.id}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </CardContent>
              </Card>
            </TabsContent>
          )}

          <TabsContent value="claims" className="space-y-4">
            <ClaimsCard title="ClaimsIdentity" description="py-identity-model ClaimsPrincipal" data={identity} />
            <ClaimsCard title="Access Token Claims" description="Validated by py-identity-model" data={accessTokenClaims} />
            <ClaimsCard title="ID Token Claims" description="Validated by py-identity-model" data={idTokenClaims} />
          </TabsContent>
        </Tabs>
      </div>
    </>
  );
}

function ClaimsCard({ title, description, data }: { title: string; description: string; data: Record<string, unknown> | null }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        {data ? (
          <pre className="rounded-lg bg-muted p-4 overflow-auto max-h-96 text-xs font-mono">
            {JSON.stringify(data, null, 2)}
          </pre>
        ) : (
          <Skeleton className="h-32 w-full" />
        )}
      </CardContent>
    </Card>
  );
}
