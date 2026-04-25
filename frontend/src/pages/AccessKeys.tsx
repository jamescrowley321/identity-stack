import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { useApiClient } from "@/hooks/useApiClient";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const EXPIRATION_OPTIONS = [
  { label: "Never", value: "0" },
  { label: "30 days", value: "30" },
  { label: "90 days", value: "90" },
  { label: "365 days", value: "365" },
];

interface AccessKey {
  id: string;
  name: string;
  status: string;
  createdTime?: number;
  expireTime?: number;
}

export default function AccessKeys() {
  const { apiFetch } = useApiClient();
  const [keys, setKeys] = useState<AccessKey[]>([]);
  const [name, setName] = useState("");
  const [expirationDays, setExpirationDays] = useState("0");
  const [newKeySecret, setNewKeySecret] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadKeys = useCallback(() => {
    apiFetch("/api/keys")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.keys) setKeys(data.keys);
      })
      .catch(() => {});
  }, [apiFetch]);

  useEffect(() => {
    loadKeys();
  }, [loadKeys]);

  const handleCreate = useCallback(async () => {
    if (!name.trim()) return;
    setNewKeySecret(null);
    const body: Record<string, unknown> = { name: name.trim() };
    const days = Number(expirationDays);
    if (days > 0) {
      body.expire_time = Math.floor(Date.now() / 1000) + days * 86400;
    }
    try {
      const res = await apiFetch("/api/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        const data = await res.json();
        setNewKeySecret(data.cleartext || null);
        setName("");
        setCopied(false);
        toast.success("Access key created");
        // Brief delay before refresh — the Descope search index needs a
        // moment to include the newly created key, otherwise loadKeys()
        // returns stale results and the list appears not to update.
        setTimeout(loadKeys, 1500);
      } else {
        const err = await res.json();
        toast.error(err.detail || "Failed to create key");
      }
    } catch {
      toast.error("Failed to create key");
    }
  }, [name, expirationDays, apiFetch, loadKeys]);

  const handleDeactivate = useCallback(
    async (keyId: string) => {
      try {
        await apiFetch(`/api/keys/${keyId}/deactivate`, { method: "POST" });
        toast.success("Key revoked");
        loadKeys();
      } catch {
        toast.error("Failed to revoke key");
      }
    },
    [apiFetch, loadKeys],
  );

  const handleActivate = useCallback(
    async (keyId: string) => {
      try {
        await apiFetch(`/api/keys/${keyId}/activate`, { method: "POST" });
        toast.success("Key activated");
        loadKeys();
      } catch {
        toast.error("Failed to activate key");
      }
    },
    [apiFetch, loadKeys],
  );

  const handleDelete = useCallback(
    async (keyId: string) => {
      try {
        await apiFetch(`/api/keys/${keyId}`, { method: "DELETE" });
        toast.success("Key deleted");
        loadKeys();
      } catch {
        toast.error("Failed to delete key");
      }
    },
    [apiFetch, loadKeys],
  );

  const copyToClipboard = useCallback(() => {
    if (newKeySecret) {
      navigator.clipboard.writeText(newKeySecret);
      setCopied(true);
      toast.success("Copied to clipboard");
    }
  }, [newKeySecret]);

  return (
    <>
      <PageHeader title="Access Keys" description="Create and manage API access keys" />
      <div className="p-8 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Create Key</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex gap-2 flex-wrap">
              <Input
                placeholder="Key name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="max-w-xs"
              />
              <Select value={expirationDays} onValueChange={setExpirationDays}>
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EXPIRATION_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>{opt.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button onClick={handleCreate} disabled={!name.trim()}>Create</Button>
            </div>
          </CardContent>
        </Card>

        {newKeySecret && (
          <Alert>
            <AlertTitle>Key created!</AlertTitle>
            <AlertDescription className="space-y-2">
              <p>Copy the secret below — it will not be shown again.</p>
              <code className="block rounded bg-muted p-2 text-xs font-mono break-all">
                {newKeySecret}
              </code>
              <Button variant="outline" size="sm" onClick={copyToClipboard}>
                {copied ? "Copied!" : "Copy to Clipboard"}
              </Button>
            </AlertDescription>
          </Alert>
        )}

        <Card>
          <CardHeader>
            <CardTitle>Keys</CardTitle>
            <CardDescription>{keys.length} key{keys.length !== 1 ? "s" : ""}</CardDescription>
          </CardHeader>
          <CardContent>
            {keys.length === 0 ? (
              <p className="text-sm text-muted-foreground">No access keys yet.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>ID</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {keys.map((k) => (
                    <TableRow key={k.id}>
                      <TableCell className="font-medium">{k.name}</TableCell>
                      <TableCell>
                        <Badge variant={k.status === "active" ? "secondary" : "destructive"}>
                          {k.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">{k.id}</TableCell>
                      <TableCell className="text-right">
                        <div className="flex gap-1 justify-end">
                          {k.status === "active" ? (
                            <Button variant="ghost" size="sm" onClick={() => handleDeactivate(k.id)}>Revoke</Button>
                          ) : (
                            <Button variant="ghost" size="sm" onClick={() => handleActivate(k.id)}>Activate</Button>
                          )}
                          <Button variant="ghost" size="sm" className="text-destructive" onClick={() => handleDelete(k.id)}>
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
      </div>
    </>
  );
}
