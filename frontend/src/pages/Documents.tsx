import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { useApiClient } from "../hooks/useApiClient";

interface Document {
  id: string;
  title: string;
  content: string;
  created_by: string;
  created_at: string;
  tenant_id: string;
}

export default function Documents() {
  const { apiFetch } = useApiClient();
  const [documents, setDocuments] = useState<Document[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [shareDocId, setShareDocId] = useState<string | null>(null);
  const [shareUserId, setShareUserId] = useState("");
  const [shareRelation, setShareRelation] = useState("viewer");

  const loadDocuments = useCallback(() => {
    apiFetch("/api/documents")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.documents) setDocuments(data.documents);
      })
      .catch(() => {});
  }, [apiFetch]);

  useEffect(() => {
    loadDocuments();
  }, [loadDocuments]);

  const handleCreate = useCallback(async () => {
    if (!title.trim()) return;
    setStatus(null);
    try {
      const res = await apiFetch("/api/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim(), content }),
      });
      if (res.ok) {
        setTitle("");
        setContent("");
        loadDocuments();
      } else {
        const err = await res.json();
        setStatus(`Error: ${err.detail || res.statusText}`);
      }
    } catch {
      setStatus("Failed to create document");
    }
  }, [title, content, apiFetch, loadDocuments]);

  const handleDelete = useCallback(
    async (docId: string) => {
      await apiFetch(`/api/documents/${docId}`, { method: "DELETE" });
      loadDocuments();
    },
    [apiFetch, loadDocuments],
  );

  const handleUpdate = useCallback(
    async (docId: string) => {
      try {
        const res = await apiFetch(`/api/documents/${docId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: editTitle, content: editContent }),
        });
        if (res.ok) {
          setEditingId(null);
          loadDocuments();
        }
      } catch {
        /* ignore */
      }
    },
    [editTitle, editContent, apiFetch, loadDocuments],
  );

  const handleShare = useCallback(
    async (docId: string) => {
      if (!shareUserId.trim()) return;
      try {
        const res = await apiFetch(`/api/documents/${docId}/share`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: shareUserId.trim(), relation: shareRelation }),
        });
        if (res.ok) {
          setShareDocId(null);
          setShareUserId("");
          setStatus("Document shared successfully");
        } else {
          const err = await res.json();
          setStatus(`Share failed: ${err.detail || res.statusText}`);
        }
      } catch {
        setStatus("Failed to share document");
      }
    },
    [shareUserId, shareRelation, apiFetch],
  );

  const startEdit = (doc: Document) => {
    setEditingId(doc.id);
    setEditTitle(doc.title);
    setEditContent(doc.content);
  };

  return (
    <div style={{ padding: "2rem", maxWidth: "900px", margin: "0 auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Documents</h1>
        <Link to="/">Back to Dashboard</Link>
      </header>
      <p style={{ color: "#666", marginBottom: "1rem" }}>
        Document-level access control via Descope FGA (ReBAC). Owners can edit, delete, and share.
      </p>

      <section style={{ marginTop: "1rem" }}>
        <h2>Create Document</h2>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", maxWidth: "400px" }}>
          <input
            type="text"
            placeholder="Title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            style={{ padding: "0.25rem 0.5rem" }}
          />
          <textarea
            placeholder="Content (optional)"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={3}
            style={{ padding: "0.25rem 0.5rem" }}
          />
          <button onClick={handleCreate} disabled={!title.trim()} style={{ alignSelf: "flex-start" }}>
            Create
          </button>
        </div>
        {status && <p style={{ marginTop: "0.5rem", color: status.startsWith("Error") ? "red" : "green" }}>{status}</p>}
      </section>

      <section style={{ marginTop: "2rem" }}>
        <h2>My Documents</h2>
        {documents.length === 0 ? (
          <p style={{ color: "#666" }}>No documents yet. Create one above or ask someone to share.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Title</th>
                <th style={{ textAlign: "left", padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Created</th>
                <th style={{ padding: "0.5rem", borderBottom: "2px solid #ddd" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.id}>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee" }}>
                    {editingId === doc.id ? (
                      <input value={editTitle} onChange={(e) => setEditTitle(e.target.value)} style={{ width: "100%" }} />
                    ) : (
                      doc.title
                    )}
                  </td>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee", fontSize: "0.8rem", color: "#666" }}>
                    {new Date(doc.created_at).toLocaleDateString()}
                  </td>
                  <td style={{ padding: "0.5rem", borderBottom: "1px solid #eee", textAlign: "right" }}>
                    {editingId === doc.id ? (
                      <>
                        <button onClick={() => handleUpdate(doc.id)}>Save</button>{" "}
                        <button onClick={() => setEditingId(null)}>Cancel</button>
                      </>
                    ) : (
                      <>
                        <button onClick={() => startEdit(doc)}>Edit</button>{" "}
                        <button onClick={() => setShareDocId(shareDocId === doc.id ? null : doc.id)}>Share</button>{" "}
                        <button onClick={() => handleDelete(doc.id)} style={{ color: "red" }}>
                          Delete
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {shareDocId && (
        <section
          style={{
            marginTop: "1rem",
            padding: "1rem",
            background: "#f0f7ff",
            border: "1px solid #2196f3",
            borderRadius: "4px",
          }}
        >
          <h3>Share Document</h3>
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            <input
              type="text"
              placeholder="User ID"
              value={shareUserId}
              onChange={(e) => setShareUserId(e.target.value)}
              style={{ padding: "0.25rem 0.5rem", minWidth: "200px" }}
            />
            <select value={shareRelation} onChange={(e) => setShareRelation(e.target.value)} style={{ padding: "0.25rem 0.5rem" }}>
              <option value="viewer">Viewer</option>
              <option value="editor">Editor</option>
            </select>
            <button onClick={() => handleShare(shareDocId)} disabled={!shareUserId.trim()}>
              Share
            </button>
            <button onClick={() => setShareDocId(null)}>Cancel</button>
          </div>
        </section>
      )}
    </div>
  );
}
