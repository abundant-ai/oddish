"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// ── Types ────────────────────────────────────────────────────────────────────

type SourceType = "paste" | "link" | "upload";

type DocumentCard = {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  source_type: string;
  source_url: string | null;
  created_by_user_id: string | null;
  updated_at: string;
};

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Read a File as base64 (without the `data:...;base64,` prefix). */
function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

// ── Sub-component: ingest (create) form ──────────────────────────────────────

interface IngestFormProps {
  onSaved: () => void;
  onCancel: () => void;
}

function DocumentIngestForm({ onSaved, onCancel }: IngestFormProps) {
  const [sourceType, setSourceType] = useState<SourceType>("paste");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSave =
    sourceType === "upload"
      ? file !== null
      : sourceType === "link"
        ? sourceUrl.trim() !== "" && content.trim() !== ""
        : content.trim() !== "";

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {
        source_type: sourceType,
      };
      if (title.trim()) payload.title = title.trim();

      if (sourceType === "upload") {
        if (!file) {
          setError("Choose a file to upload.");
          return;
        }
        payload.file_b64 = await readFileAsBase64(file);
        payload.raw_filename = file.name;
        payload.raw_mime = file.type || "application/octet-stream";
      } else {
        payload.content = content;
        if (sourceType === "link") payload.source_url = sourceUrl.trim();
      }

      const res = await fetch("/api/documents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        let detail = "Failed to create document";
        try {
          const data = (await res.json()) as { detail?: string };
          detail = data.detail ?? detail;
        } catch {
          // keep default
        }
        setError(detail);
        return;
      }

      onSaved();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  const tabs: { key: SourceType; label: string }[] = [
    { key: "paste", label: "Paste text" },
    { key: "link", label: "Link" },
    { key: "upload", label: "Upload file" },
  ];

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">New document</CardTitle>
        <p className="text-[11px] text-muted-foreground">
          A summary, digest, and tags are generated automatically for agent
          retrieval.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Source-type toggle */}
        <div className="flex items-center gap-1">
          {tabs.map((t) => (
            <Button
              key={t.key}
              type="button"
              variant={sourceType === t.key ? "secondary" : "ghost"}
              size="sm"
              className="h-7 text-xs border border-transparent data-[active=true]:border-[#85b85c]/25"
              data-active={sourceType === t.key}
              onClick={() => setSourceType(t.key)}
            >
              {t.label}
            </Button>
          ))}
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="doc-title" className="text-xs font-medium">
            Title{" "}
            <span className="text-muted-foreground">(optional)</span>
          </Label>
          <Input
            id="doc-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Derived from the filename or first line if blank"
            maxLength={200}
            className="h-8 border-[#6f88b4]/20"
          />
        </div>

        {sourceType === "link" && (
          <div className="space-y-1.5">
            <Label htmlFor="doc-url" className="text-xs font-medium">
              Source URL
            </Label>
            <Input
              id="doc-url"
              value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)}
              placeholder="https://…"
              className="h-8 border-[#6f88b4]/20"
            />
          </div>
        )}

        {sourceType === "upload" ? (
          <div className="space-y-1.5">
            <Label htmlFor="doc-file" className="text-xs font-medium">
              File <span className="text-muted-foreground">(PDF, text, markdown)</span>
            </Label>
            <input
              id="doc-file"
              type="file"
              accept=".pdf,.txt,.md,.markdown,text/*,application/pdf"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="block w-full text-xs text-muted-foreground file:mr-3 file:rounded-md file:border file:border-[#6f88b4]/20 file:bg-muted file:px-3 file:py-1.5 file:text-xs"
            />
          </div>
        ) : (
          <div className="space-y-1.5">
            <Label htmlFor="doc-content" className="text-xs font-medium">
              {sourceType === "link" ? "Notes / excerpt" : "Content"}
            </Label>
            <textarea
              id="doc-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={10}
              placeholder={
                sourceType === "link"
                  ? "Paste the relevant content or your notes about this link…"
                  : "Paste the document text…"
              }
              className="w-full rounded-md border border-[#6f88b4]/20 bg-background px-3 py-2 font-mono text-sm resize-y focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertTitle>Error</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <div className="flex items-center gap-2 pt-1">
          <Button
            type="button"
            size="sm"
            onClick={() => void handleSave()}
            disabled={saving || !canSave}
            className="h-8"
          >
            {saving ? "Ingesting…" : "Add document"}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 border-[#6f88b4]/20"
            onClick={onCancel}
            disabled={saving}
          >
            Cancel
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Sub-component: edit (metadata) form ──────────────────────────────────────

interface EditFormProps {
  doc: DocumentCard;
  onSaved: () => void;
  onCancel: () => void;
}

function DocumentEditForm({ doc, onSaved, onCancel }: EditFormProps) {
  const [title, setTitle] = useState(doc.title);
  const [summary, setSummary] = useState(doc.summary);
  const [tags, setTags] = useState(doc.tags.join(", "));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const payload = {
        title: title.trim(),
        summary: summary.trim(),
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter((t) => t !== ""),
      };
      const res = await fetch(`/api/documents/${doc.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let detail = "Failed to update document";
        try {
          const data = (await res.json()) as { detail?: string };
          detail = data.detail ?? detail;
        } catch {
          // keep default
        }
        setError(detail);
        return;
      }
      onSaved();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Edit document</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="edit-title" className="text-xs font-medium">
            Title
          </Label>
          <Input
            id="edit-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={200}
            className="h-8 border-[#6f88b4]/20"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="edit-summary" className="text-xs font-medium">
            Summary
          </Label>
          <textarea
            id="edit-summary"
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            rows={3}
            className="w-full rounded-md border border-[#6f88b4]/20 bg-background px-3 py-2 text-sm resize-y focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="edit-tags" className="text-xs font-medium">
            Tags <span className="text-muted-foreground">(comma-separated)</span>
          </Label>
          <Input
            id="edit-tags"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="aws, onboarding, qa"
            className="h-8 border-[#6f88b4]/20"
          />
        </div>

        {error && (
          <Alert variant="destructive">
            <AlertTitle>Error</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <div className="flex items-center gap-2 pt-1">
          <Button
            type="button"
            size="sm"
            onClick={() => void handleSave()}
            disabled={saving || !title.trim()}
            className="h-8"
          >
            {saving ? "Saving…" : "Update document"}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 border-[#6f88b4]/20"
            onClick={onCancel}
            disabled={saving}
          >
            Cancel
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Main client component ─────────────────────────────────────────────────────

export function DocumentsClient() {
  const [documents, setDocuments] = useState<DocumentCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // null = list; "new" = ingest form; DocumentCard = edit form
  const [formState, setFormState] = useState<"new" | DocumentCard | null>(null);

  const loadDocuments = useCallback(async () => {
    setLoading(true);
    setFetchError(null);
    try {
      const res = await fetch("/api/documents");
      if (!res.ok) {
        setFetchError(`Failed to load documents (HTTP ${res.status})`);
        return;
      }
      const data = (await res.json()) as DocumentCard[];
      setDocuments(data);
    } catch (err: unknown) {
      setFetchError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDocuments();
  }, [loadDocuments]);

  async function handleDelete(doc: DocumentCard) {
    if (!confirm(`Delete "${doc.title}"? This cannot be undone.`)) {
      return;
    }
    try {
      const res = await fetch(`/api/documents/${doc.id}`, { method: "DELETE" });
      if (!res.ok) {
        alert(`Failed to delete document (HTTP ${res.status})`);
        return;
      }
      await loadDocuments();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : String(err));
    }
  }

  function handleFormSaved() {
    setFormState(null);
    void loadDocuments();
  }

  function handleFormCancel() {
    setFormState(null);
  }

  if (formState !== null) {
    return (
      <div className="space-y-4">
        {formState === "new" ? (
          <DocumentIngestForm
            onSaved={handleFormSaved}
            onCancel={handleFormCancel}
          />
        ) : (
          <DocumentEditForm
            doc={formState}
            onSaved={handleFormSaved}
            onCancel={handleFormCancel}
          />
        )}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1">
            <CardTitle className="text-base">Documents</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              Reference docs retrievable by Claude Code agents via the doc-store
              MCP server.
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            className="h-8 text-xs"
            onClick={() => setFormState("new")}
          >
            + New document
          </Button>
        </CardHeader>
        <CardContent>
          {fetchError ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load documents</AlertTitle>
              <AlertDescription>{fetchError}</AlertDescription>
            </Alert>
          ) : loading ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              Loading…
            </div>
          ) : documents.length === 0 ? (
            <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground">
              No documents yet. Add one to get started.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[200px]">Title</TableHead>
                  <TableHead>Summary</TableHead>
                  <TableHead className="w-[160px]">Tags</TableHead>
                  <TableHead className="w-[80px]">Source</TableHead>
                  <TableHead className="w-[120px] text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {documents.map((doc) => (
                  <TableRow key={doc.id}>
                    <TableCell className="text-xs font-medium">
                      {doc.source_url ? (
                        <a
                          href={doc.source_url}
                          target="_blank"
                          rel="noreferrer"
                          className="hover:underline"
                        >
                          {doc.title}
                        </a>
                      ) : (
                        doc.title
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {doc.summary || <span className="italic">No summary</span>}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {doc.tags.slice(0, 4).map((tag) => (
                          <Badge
                            key={tag}
                            variant="outline"
                            className="text-[10px] px-1.5 py-0 border-[#6f88b4]/30"
                          >
                            {tag}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell className="text-[11px] text-muted-foreground">
                      {doc.source_type}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-6 px-2 text-[11px]"
                          onClick={() => setFormState(doc)}
                        >
                          Edit
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="h-6 px-2 text-[11px] text-red-500 hover:text-red-600 hover:bg-red-500/10"
                          onClick={() => void handleDelete(doc)}
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
  );
}
