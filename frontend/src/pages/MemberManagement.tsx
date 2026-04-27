import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { useApiClient } from "@/hooks/useApiClient";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const AVAILABLE_ROLES = ["owner", "admin", "member", "viewer"];

interface Member {
  userId: string;
  name?: string;
  email?: string;
  status?: string;
  roleNames?: string[];
}

export default function MemberManagement() {
  const { apiFetch } = useApiClient();
  const [members, setMembers] = useState<Member[]>([]);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("member");

  const loadMembers = useCallback(() => {
    apiFetch("/api/members")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.members) setMembers(data.members);
      })
      .catch(() => {});
  }, [apiFetch]);

  useEffect(() => {
    loadMembers();
  }, [loadMembers]);

  const handleInvite = useCallback(async () => {
    if (!inviteEmail.trim()) return;
    try {
      const res = await apiFetch("/api/members/invite", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: inviteEmail.trim(), role_names: [inviteRole] }),
      });
      if (res.ok) {
        toast.success(`Invited ${inviteEmail.trim()}`);
        setInviteEmail("");
        loadMembers();
      } else {
        const err = await res.json();
        toast.error(err.detail || res.statusText);
      }
    } catch {
      toast.error("Failed to invite");
    }
  }, [inviteEmail, inviteRole, apiFetch, loadMembers]);

  const handleToggleStatus = useCallback(
    async (userId: string, currentStatus: string) => {
      const action = currentStatus === "enabled" ? "deactivate" : "activate";
      try {
        await apiFetch(`/api/members/${userId}/${action}`, { method: "POST" });
        toast.success(`Member ${action}d`);
        loadMembers();
      } catch {
        toast.error(`Failed to ${action} member`);
      }
    },
    [apiFetch, loadMembers],
  );

  const handleRemove = useCallback(
    async (userId: string) => {
      try {
        await apiFetch(`/api/members/${userId}`, { method: "DELETE" });
        toast.success("Member removed");
        loadMembers();
      } catch {
        toast.error("Failed to remove member");
      }
    },
    [apiFetch, loadMembers],
  );

  return (
    <>
      <PageHeader title="Members" description="Invite and manage team members" />
      <div className="p-8 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Invite Member</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex gap-2 flex-wrap">
              <Input
                type="email"
                placeholder="Email address"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                className="max-w-xs"
              />
              <Select value={inviteRole} onValueChange={setInviteRole}>
                <SelectTrigger className="w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {AVAILABLE_ROLES.map((r) => (
                    <SelectItem key={r} value={r}>{r}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button onClick={handleInvite} disabled={!inviteEmail.trim()}>Invite</Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Team</CardTitle>
            <CardDescription>{members.length} member{members.length !== 1 ? "s" : ""}</CardDescription>
          </CardHeader>
          <CardContent>
            {members.length === 0 ? (
              <p className="text-sm text-muted-foreground">No members found.</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name / Email</TableHead>
                    <TableHead>Roles</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {members.map((m) => (
                    <TableRow key={m.userId}>
                      <TableCell>
                        <div className="font-medium">{m.name || m.email || m.userId}</div>
                        {m.email && m.name && (
                          <div className="text-xs text-muted-foreground">{m.email}</div>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex gap-1 flex-wrap">
                          {(m.roleNames || []).map((r) => (
                            <Badge key={r} variant="outline">{r}</Badge>
                          ))}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={m.status === "enabled" ? "secondary" : "destructive"}>
                          {m.status || "unknown"}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex gap-1 justify-end">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => handleToggleStatus(m.userId, m.status || "enabled")}
                          >
                            {m.status === "enabled" ? "Deactivate" : "Activate"}
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="text-destructive"
                            onClick={() => handleRemove(m.userId)}
                          >
                            Remove
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
