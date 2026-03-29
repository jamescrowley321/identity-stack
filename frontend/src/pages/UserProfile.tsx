import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { useApiClient } from "@/hooks/useApiClient";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";

const USER_ATTRIBUTES = ["department", "job_title", "avatar_url"];

interface ProfileData {
  user_id: string;
  name: string;
  email: string;
  custom_attributes: Record<string, string>;
}

export default function UserProfile() {
  const { apiFetch } = useApiClient();
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [editKey, setEditKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  useEffect(() => {
    apiFetch("/api/profile")
      .then((res) => (res.ok ? res.json() : null))
      .then(setProfile)
      .catch(() => {});
  }, [apiFetch]);

  const handleSave = useCallback(async () => {
    if (!editKey) return;
    try {
      const res = await apiFetch("/api/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: editKey, value: editValue }),
      });
      if (res.ok) {
        setProfile((prev) =>
          prev ? { ...prev, custom_attributes: { ...prev.custom_attributes, [editKey]: editValue } } : prev,
        );
        setEditKey(null);
        toast.success("Attribute saved");
      } else {
        const err = await res.json();
        toast.error(err.detail || res.statusText);
      }
    } catch {
      toast.error("Failed to save");
    }
  }, [editKey, editValue, apiFetch]);

  if (!profile) {
    return (
      <>
        <PageHeader title="User Profile" />
        <div className="p-6 space-y-6">
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-48 w-full" />
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader title="User Profile" description={profile.email || undefined} />
      <div className="p-6 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Profile</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Name:</span>
              <span className="text-sm font-medium">{profile.name || "Not set"}</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Email:</span>
              <span className="text-sm font-medium">{profile.email || "Not set"}</span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Custom Attributes</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Attribute</TableHead>
                  <TableHead>Value</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {USER_ATTRIBUTES.map((key) => (
                  <TableRow key={key}>
                    <TableCell className="font-medium">{key}</TableCell>
                    <TableCell>
                      {editKey === key ? (
                        <Input
                          value={editValue}
                          onChange={(e) => setEditValue(e.target.value)}
                          className="max-w-xs"
                        />
                      ) : (
                        profile.custom_attributes[key] || (
                          <span className="text-muted-foreground">Not set</span>
                        )
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {editKey === key ? (
                        <div className="flex gap-1 justify-end">
                          <Button size="sm" onClick={handleSave}>Save</Button>
                          <Button variant="outline" size="sm" onClick={() => setEditKey(null)}>Cancel</Button>
                        </div>
                      ) : (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => { setEditKey(key); setEditValue(profile.custom_attributes[key] || ""); }}
                        >
                          Edit
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
