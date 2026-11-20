import { useEffect, useMemo, useState, useCallback, useRef } from "react";
import { toast } from "sonner";
import { useApiClient } from "@/hooks/useApiClient";
import { useRBAC } from "@/hooks/useRBAC";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// No "Never" option: the API rejects an unbounded key, and offering it here made
// a permanent credential the default choice. 365 is the ceiling the API enforces
// (MAX_ACCESS_KEY_LIFETIME_SECONDS).
const EXPIRATION_OPTIONS = [
  { label: "30 days", value: "30" },
  { label: "90 days", value: "90" },
  { label: "365 days", value: "365" },
];

const DEFAULT_EXPIRATION_DAYS = "90";

// Roles the API refuses on any key (it would open every tenant's state to it).
const ROLES_NOT_FOR_KEYS = new Set(["operator"]);

interface AccessKey {
  id: string;
  name: string;
  status: string;
  createdTime?: number;
  expireTime?: number;
}

export default function AccessKeys() {
  const { apiFetch } = useApiClient();
  const [name, setName] = useState("");
  const [expirationDays, setExpirationDays] = useState(DEFAULT_EXPIRATION_DAYS);
  // A key carries one role the user holds in this tenant: the API refuses a key
  // with none, a role the user lacks, and operator.
  const { roles } = useRBAC();
  const grantable = useMemo(() => roles.filter((option) => !ROLES_NOT_FOR_KEYS.has(option)), [roles]);
  const [chosenRole, setChosenRole] = useState<string | null>(null);
  const role = useMemo(
    () => (chosenRole !== null && grantable.includes(chosenRole) ? chosenRole : (grantable[0] ?? null)),
    [chosenRole, grantable],
  );
  const [newKeySecret, setNewKeySecret] = useState<string | null>(null);
  // The client id a canonical provider's credential signs in with (client
  // credentials grant); a Descope access key has none, its secret is the key.
  const [newKeyClientId, setNewKeyClientId] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  // The page is mounted afresh for each tenant, so once it has gone, a call it
  // started finishes in a tenant the user has left: nothing more is loaded or
  // shown there, and no toast is shown, since the shell's toaster outlives the
  // page and the toast would read as if the call ran in the tenant now on screen.
  // The one exception is a key created after the page went (see handleCreate).
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // The list reloads when apiFetch changes. Only the latest load fills it, so
  // an earlier answer arriving late is dropped. Rows are kept only for the
  // apiFetch that loaded them; a failed load keeps them and says so.
  const [loaded, setLoaded] = useState<{ by: typeof apiFetch | null; keys: AccessKey[] }>({ by: null, keys: [] });
  const keys = loaded.by === apiFetch ? loaded.keys : [];
  const latestLoad = useRef(0);
  const delayedReload = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const loadKeys = useCallback(() => {
    if (!mounted.current) return;
    const load = ++latestLoad.current;
    const isLatest = () => mounted.current && load === latestLoad.current;
    apiFetch("/api/keys")
      .then(async (res) => {
        if (!res.ok) throw new Error(`keys load failed: ${res.status}`);
        const data = await res.json();
        if (isLatest()) setLoaded({ by: apiFetch, keys: Array.isArray(data?.keys) ? data.keys : [] });
      })
      .catch(() => {
        if (isLatest()) toast.error("Failed to load access keys");
      });
  }, [apiFetch]);

  useEffect(() => {
    loadKeys();
    return () => clearTimeout(delayedReload.current);
  }, [loadKeys]);

  const handleCreate = useCallback(async () => {
    if (!name.trim() || role === null) return;
    setNewKeySecret(null);
    setNewKeyClientId(null);
    // Always send an expiry. Omitting it used to mean "never expires", which the
    // API now refuses.
    const days = Number(expirationDays) || Number(DEFAULT_EXPIRATION_DAYS);
    const body: Record<string, unknown> = {
      name: name.trim(),
      expire_time: Math.floor(Date.now() / 1000) + days * 86400,
      role_names: [role],
    };
    try {
      const res = await apiFetch("/api/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        const data = await res.json();
        if (!mounted.current) {
          // The page went (a tenant switch or another page) while the key was
          // created: its secret, shown only once, has nowhere to be shown.
          toast.warning(
            "An access key was created after you left this page. Its secret can't be shown: revoke the key if you need it.",
          );
          return;
        }
        setNewKeySecret(data.cleartext || null);
        setNewKeyClientId(typeof data.key?.clientId === "string" ? data.key.clientId : null);
        setName("");
        setCopied(false);
        toast.success("Access key created");
        // Brief delay before refresh — the Descope search index needs a
        // moment to include the newly created key, otherwise loadKeys()
        // returns stale results and the list appears not to update.
        // One pending reload at a time, so the cleanup below always holds its timer.
        clearTimeout(delayedReload.current);
        delayedReload.current = setTimeout(loadKeys, 1500);
      } else {
        const err = await res.json();
        if (mounted.current) toast.error(err.detail || "Failed to create key");
      }
    } catch {
      if (mounted.current) toast.error("Failed to create key");
    }
  }, [name, role, expirationDays, apiFetch, loadKeys]);

  const handleDeactivate = useCallback(
    async (keyId: string) => {
      try {
        await apiFetch(`/api/keys/${keyId}/deactivate`, { method: "POST" });
        if (!mounted.current) return;
        toast.success("Key revoked");
        loadKeys();
      } catch {
        if (mounted.current) toast.error("Failed to revoke key");
      }
    },
    [apiFetch, loadKeys],
  );

  const handleActivate = useCallback(
    async (keyId: string) => {
      try {
        await apiFetch(`/api/keys/${keyId}/activate`, { method: "POST" });
        if (!mounted.current) return;
        toast.success("Key activated");
        loadKeys();
      } catch {
        if (mounted.current) toast.error("Failed to activate key");
      }
    },
    [apiFetch, loadKeys],
  );

  const handleDelete = useCallback(
    async (keyId: string) => {
      try {
        await apiFetch(`/api/keys/${keyId}`, { method: "DELETE" });
        if (!mounted.current) return;
        toast.success("Key deleted");
        loadKeys();
      } catch {
        if (mounted.current) toast.error("Failed to delete key");
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
      <PageHeader
        title="Access Keys"
        description="Create and manage API access keys"
      />
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
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={role ?? undefined} onValueChange={setChosenRole}>
                <SelectTrigger className="w-32" aria-label="Key role">
                  <SelectValue placeholder="No role" />
                </SelectTrigger>
                <SelectContent>
                  {grantable.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button onClick={handleCreate} disabled={!name.trim() || role === null}>
                Create
              </Button>
            </div>
          </CardContent>
        </Card>

        {newKeySecret && (
          <Alert>
            <AlertTitle>Key created!</AlertTitle>
            <AlertDescription className="space-y-2">
              {newKeyClientId && (
                <p>
                  Client ID:{" "}
                  <code className="rounded bg-muted px-1 text-xs font-mono">{newKeyClientId}</code>
                </p>
              )}
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
            <CardDescription>
              {keys.length} key{keys.length !== 1 ? "s" : ""}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {keys.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No access keys yet.
              </p>
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
                        <Badge
                          variant={
                            k.status === "active" ? "secondary" : "destructive"
                          }
                        >
                          {k.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {k.id}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex gap-1 justify-end">
                          {k.status === "active" ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => handleDeactivate(k.id)}
                            >
                              Revoke
                            </Button>
                          ) : (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => handleActivate(k.id)}
                            >
                              Activate
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            className="text-destructive"
                            onClick={() => handleDelete(k.id)}
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
      </div>
    </>
  );
}
