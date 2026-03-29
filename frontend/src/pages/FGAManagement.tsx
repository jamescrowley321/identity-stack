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
import { Skeleton } from "@/components/ui/skeleton";

interface RelationInfo {
  target: string;
  relationDefinition: string;
}

async function parseErrorDetail(res: Response): Promise<string | undefined> {
  try {
    const data = await res.json();
    return data.detail;
  } catch {
    return undefined;
  }
}

export default function FGAManagement() {
  const { apiFetch } = useApiClient();
  const { isAdmin } = useRBAC();

  // Schema state
  const [schema, setSchema] = useState("");
  const [schemaLoading, setSchemaLoading] = useState(true);

  // Relations state
  const [relResourceType, setRelResourceType] = useState("");
  const [relResourceId, setRelResourceId] = useState("");
  const [relations, setRelations] = useState<RelationInfo[]>([]);
  const [relationsLoading, setRelationsLoading] = useState(false);
  const [relationsQueried, setRelationsQueried] = useState(false);

  // Create relation state
  const [createResourceType, setCreateResourceType] = useState("");
  const [createResourceId, setCreateResourceId] = useState("");
  const [createRelation, setCreateRelation] = useState("");
  const [createTarget, setCreateTarget] = useState("");

  // Auth check state
  const [checkResourceType, setCheckResourceType] = useState("");
  const [checkResourceId, setCheckResourceId] = useState("");
  const [checkRelation, setCheckRelation] = useState("");
  const [checkTarget, setCheckTarget] = useState("");
  const [checkResult, setCheckResult] = useState<boolean | null>(null);

  // Mutation loading state
  const [saving, setSaving] = useState(false);

  // Load schema
  const loadSchema = useCallback(() => {
    setSchemaLoading(true);
    apiFetch("/api/fga/schema")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data) setSchema(data.schema || "");
      })
      .catch(() => {
        toast.error("Failed to load FGA schema");
      })
      .finally(() => setSchemaLoading(false));
  }, [apiFetch]);

  useEffect(() => {
    if (isAdmin) {
      loadSchema();
    }
  }, [isAdmin, loadSchema]);

  // Browse relations
  const handleBrowseRelations = useCallback(async () => {
    if (!relResourceType.trim() || !relResourceId.trim()) return;
    setRelationsLoading(true);
    setRelationsQueried(true);
    try {
      const params = new URLSearchParams({
        resource_type: relResourceType.trim(),
        resource_id: relResourceId.trim(),
      });
      const res = await apiFetch(`/api/fga/relations?${params}`);
      if (res.ok) {
        const data = await res.json();
        setRelations(data.relations || []);
      } else {
        const detail = await parseErrorDetail(res);
        toast.error(detail || "Failed to load relations");
        setRelations([]);
      }
    } catch {
      toast.error("Failed to load relations");
      setRelations([]);
    } finally {
      setRelationsLoading(false);
    }
  }, [relResourceType, relResourceId, apiFetch]);

  // Delete relation
  const handleDeleteRelation = useCallback(
    async (rel: RelationInfo) => {
      if (saving) return;
      setSaving(true);
      try {
        const res = await apiFetch("/api/fga/relations", {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            resource_type: relResourceType.trim(),
            resource_id: relResourceId.trim(),
            relation: rel.relationDefinition,
            target: rel.target,
          }),
        });
        if (res.ok) {
          toast.success("Relation deleted");
          handleBrowseRelations();
        } else {
          const detail = await parseErrorDetail(res);
          toast.error(detail || "Failed to delete relation");
        }
      } catch {
        toast.error("Failed to delete relation");
      } finally {
        setSaving(false);
      }
    },
    [saving, relResourceType, relResourceId, apiFetch, handleBrowseRelations],
  );

  // Create relation
  const handleCreateRelation = useCallback(async () => {
    if (!createResourceType.trim() || !createResourceId.trim() || !createRelation.trim() || !createTarget.trim() || saving) return;
    setSaving(true);
    try {
      const res = await apiFetch("/api/fga/relations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          resource_type: createResourceType.trim(),
          resource_id: createResourceId.trim(),
          relation: createRelation.trim(),
          target: createTarget.trim(),
        }),
      });
      if (res.ok) {
        toast.success("Relation created");
        setCreateResourceType("");
        setCreateResourceId("");
        setCreateRelation("");
        setCreateTarget("");
      } else {
        const detail = await parseErrorDetail(res);
        toast.error(detail || "Failed to create relation");
      }
    } catch {
      toast.error("Failed to create relation");
    } finally {
      setSaving(false);
    }
  }, [createResourceType, createResourceId, createRelation, createTarget, saving, apiFetch]);

  // Check permission
  const handleCheckPermission = useCallback(async () => {
    if (!checkResourceType.trim() || !checkResourceId.trim() || !checkRelation.trim() || !checkTarget.trim() || saving) return;
    setSaving(true);
    setCheckResult(null);
    try {
      const res = await apiFetch("/api/fga/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          resource_type: checkResourceType.trim(),
          resource_id: checkResourceId.trim(),
          relation: checkRelation.trim(),
          target: checkTarget.trim(),
        }),
      });
      if (res.ok) {
        const data = await res.json();
        setCheckResult(data.allowed);
      } else {
        const detail = await parseErrorDetail(res);
        toast.error(detail || "Failed to check permission");
      }
    } catch {
      toast.error("Failed to check permission");
    } finally {
      setSaving(false);
    }
  }, [checkResourceType, checkResourceId, checkRelation, checkTarget, saving, apiFetch]);

  return (
    <>
      <PageHeader title="FGA Management" description="Fine-Grained Authorization schema, relations, and permission checks" />
      <div className="p-6 space-y-6">
        {isAdmin ? (
          <>
            {/* Schema Viewer */}
            <Card>
              <CardHeader>
                <CardTitle>Authorization Schema</CardTitle>
                <CardDescription>Current FGA schema definition (read-only)</CardDescription>
              </CardHeader>
              <CardContent>
                {schemaLoading ? (
                  <div className="space-y-2">
                    <Skeleton className="h-4 w-full" />
                    <Skeleton className="h-4 w-3/4" />
                    <Skeleton className="h-4 w-1/2" />
                    <Skeleton className="h-4 w-full" />
                    <Skeleton className="h-4 w-2/3" />
                  </div>
                ) : schema ? (
                  <pre className="bg-muted p-4 rounded-md text-sm overflow-x-auto whitespace-pre-wrap">{schema}</pre>
                ) : (
                  <p className="text-sm text-muted-foreground">No schema defined.</p>
                )}
              </CardContent>
            </Card>

            {/* Browse Relations */}
            <Card>
              <CardHeader>
                <CardTitle>Relations</CardTitle>
                <CardDescription>Browse and delete FGA relation tuples for a resource</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex gap-2 flex-wrap items-end">
                  <div className="space-y-1">
                    <Label htmlFor="rel-resource-type">Resource Type</Label>
                    <Input
                      id="rel-resource-type"
                      placeholder="e.g. document"
                      value={relResourceType}
                      onChange={(e) => setRelResourceType(e.target.value)}
                      className="w-48"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="rel-resource-id">Resource ID</Label>
                    <Input
                      id="rel-resource-id"
                      placeholder="e.g. doc-123"
                      value={relResourceId}
                      onChange={(e) => setRelResourceId(e.target.value)}
                      className="w-48"
                    />
                  </div>
                  <Button
                    onClick={handleBrowseRelations}
                    disabled={!relResourceType.trim() || !relResourceId.trim()}
                  >
                    Browse
                  </Button>
                </div>
                {relationsLoading ? (
                  <div className="space-y-2">
                    <Skeleton className="h-8 w-full" />
                    <Skeleton className="h-8 w-full" />
                  </div>
                ) : relationsQueried ? (
                  relations.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No relations found.</p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Relation</TableHead>
                          <TableHead>Target</TableHead>
                          <TableHead className="text-right">Actions</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {relations.map((rel, idx) => (
                          <TableRow key={`${rel.relationDefinition}-${rel.target}-${idx}`}>
                            <TableCell className="font-medium">{rel.relationDefinition}</TableCell>
                            <TableCell>{rel.target}</TableCell>
                            <TableCell className="text-right">
                              <Button
                                variant="ghost"
                                size="sm"
                                className="text-destructive"
                                onClick={() => handleDeleteRelation(rel)}
                                disabled={saving}
                              >
                                Delete
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )
                ) : null}
              </CardContent>
            </Card>

            {/* Create Relation */}
            <Card>
              <CardHeader>
                <CardTitle>Create Relation</CardTitle>
                <CardDescription>Add a new FGA relation tuple</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="flex gap-2 flex-wrap items-end">
                  <div className="space-y-1">
                    <Label htmlFor="create-resource-type">Resource Type</Label>
                    <Input
                      id="create-resource-type"
                      placeholder="e.g. document"
                      value={createResourceType}
                      onChange={(e) => setCreateResourceType(e.target.value)}
                      className="w-40"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="create-resource-id">Resource ID</Label>
                    <Input
                      id="create-resource-id"
                      placeholder="e.g. doc-123"
                      value={createResourceId}
                      onChange={(e) => setCreateResourceId(e.target.value)}
                      className="w-40"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="create-relation">Relation</Label>
                    <Input
                      id="create-relation"
                      placeholder="e.g. viewer"
                      value={createRelation}
                      onChange={(e) => setCreateRelation(e.target.value)}
                      className="w-40"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="create-target">Target</Label>
                    <Input
                      id="create-target"
                      placeholder="e.g. user:abc123"
                      value={createTarget}
                      onChange={(e) => setCreateTarget(e.target.value)}
                      className="w-48"
                    />
                  </div>
                  <Button
                    onClick={handleCreateRelation}
                    disabled={!createResourceType.trim() || !createResourceId.trim() || !createRelation.trim() || !createTarget.trim() || saving}
                  >
                    Create
                  </Button>
                </div>
              </CardContent>
            </Card>

            {/* Authorization Check */}
            <Card>
              <CardHeader>
                <CardTitle>Authorization Check</CardTitle>
                <CardDescription>Test whether a target has a permission on a resource</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="flex gap-2 flex-wrap items-end">
                  <div className="space-y-1">
                    <Label htmlFor="check-resource-type">Resource Type</Label>
                    <Input
                      id="check-resource-type"
                      placeholder="e.g. document"
                      value={checkResourceType}
                      onChange={(e) => setCheckResourceType(e.target.value)}
                      className="w-40"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="check-resource-id">Resource ID</Label>
                    <Input
                      id="check-resource-id"
                      placeholder="e.g. doc-123"
                      value={checkResourceId}
                      onChange={(e) => setCheckResourceId(e.target.value)}
                      className="w-40"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="check-relation">Relation</Label>
                    <Input
                      id="check-relation"
                      placeholder="e.g. can_view"
                      value={checkRelation}
                      onChange={(e) => setCheckRelation(e.target.value)}
                      className="w-40"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="check-target">Target</Label>
                    <Input
                      id="check-target"
                      placeholder="e.g. user:abc123"
                      value={checkTarget}
                      onChange={(e) => setCheckTarget(e.target.value)}
                      className="w-48"
                    />
                  </div>
                  <Button
                    onClick={handleCheckPermission}
                    disabled={!checkResourceType.trim() || !checkResourceId.trim() || !checkRelation.trim() || !checkTarget.trim() || saving}
                  >
                    Check
                  </Button>
                </div>
                {checkResult !== null && (
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">Result:</span>
                    <Badge variant={checkResult ? "default" : "destructive"}>
                      {checkResult ? "Allowed" : "Denied"}
                    </Badge>
                  </div>
                )}
              </CardContent>
            </Card>
          </>
        ) : (
          <Alert>
            <AlertDescription>
              You need an admin or owner role to manage FGA.
            </AlertDescription>
          </Alert>
        )}
      </div>
    </>
  );
}
