import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { useApiClient } from "@/hooks/useApiClient";
import { useRBAC } from "@/hooks/useRBAC";
import { Unauthorized } from "@/components/Unauthorized";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";

interface TenantSettingsData {
  tenant_id: string;
  name: string;
  custom_attributes: Record<string, string | number | boolean>;
}

const PLAN_TIERS = ["free", "pro", "enterprise"];

export default function TenantSettings() {
  const { apiFetch } = useApiClient();
  const { isAdmin } = useRBAC();
  const [settings, setSettings] = useState<TenantSettingsData | null>(null);
  const [unauthorized, setUnauthorized] = useState(false);
  const [planTier, setPlanTier] = useState("free");
  const [maxMembers, setMaxMembers] = useState("10");

  useEffect(() => {
    apiFetch("/api/tenants/current/settings")
      .then((res) => {
        if (res.status === 403) {
          setUnauthorized(true);
          return null;
        }
        return res.ok ? res.json() : null;
      })
      .then((data) => {
        if (data) {
          setSettings(data);
          setPlanTier(String(data.custom_attributes?.plan_tier ?? "free"));
          setMaxMembers(String(data.custom_attributes?.max_members ?? "10"));
        }
      })
      .catch(() => {});
  }, [apiFetch]);

  const handleSave = useCallback(async () => {
    try {
      const res = await apiFetch("/api/tenants/current/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          custom_attributes: { plan_tier: planTier, max_members: parseInt(maxMembers, 10) || 10 },
        }),
      });
      if (res.ok) {
        toast.success("Settings saved");
        setSettings((prev) =>
          prev
            ? { ...prev, custom_attributes: { ...prev.custom_attributes, plan_tier: planTier, max_members: parseInt(maxMembers, 10) || 10 } }
            : prev,
        );
      } else {
        const err = await res.json();
        toast.error(err.detail || res.statusText);
      }
    } catch {
      toast.error("Failed to save");
    }
  }, [planTier, maxMembers, apiFetch]);

  if (unauthorized) return <Unauthorized />;

  if (!settings) {
    return (
      <>
        <PageHeader title="Tenant Settings" />
        <div className="p-6 space-y-6">
          <Skeleton className="h-32 w-full" />
        </div>
      </>
    );
  }

  const attrs = settings.custom_attributes;

  return (
    <>
      <PageHeader title="Tenant Settings" description={settings.name || settings.tenant_id} />
      <div className="p-6 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Current Settings</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Plan:</span>
              <Badge variant="outline">{String(attrs.plan_tier ?? "free")}</Badge>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Max Members:</span>
              <span className="text-sm font-medium">{String(attrs.max_members ?? "Not set")}</span>
            </div>
          </CardContent>
        </Card>

        {isAdmin && (
          <Card>
            <CardHeader>
              <CardTitle>Edit Settings</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex gap-4 flex-wrap items-end">
                <div className="space-y-1">
                  <Label>Plan Tier</Label>
                  <Select value={planTier} onValueChange={setPlanTier}>
                    <SelectTrigger className="w-36">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {PLAN_TIERS.map((t) => (
                        <SelectItem key={t} value={t}>{t}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <Label>Max Members</Label>
                  <Input
                    type="number"
                    value={maxMembers}
                    onChange={(e) => setMaxMembers(e.target.value)}
                    className="w-24"
                    min="1"
                  />
                </div>
                <Button onClick={handleSave}>Save</Button>
              </div>
            </CardContent>
          </Card>
        )}

        {!isAdmin && (
          <Alert>
            <AlertDescription>
              Contact an admin to change tenant settings.
            </AlertDescription>
          </Alert>
        )}
      </div>
    </>
  );
}
