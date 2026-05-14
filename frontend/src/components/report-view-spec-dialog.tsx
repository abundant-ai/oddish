"use client";

import { useState } from "react";
import { Code2, Download, Loader2 } from "lucide-react";
import { CodeBlock } from "@/components/code-block";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface Props {
  reportId: string;
}

/**
 * "View spec" button + dialog. Fetches the canonical YAML rendering
 * via the existing ``GET /reports/{id}.yaml`` endpoint and renders it
 * through the shared ``CodeBlock`` component (shiki highlighting + copy
 * button + download).
 */
export function ReportViewSpecDialog({ reportId }: Props) {
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadOnOpen(next: boolean) {
    setOpen(next);
    if (!next || body !== null) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/reports/${reportId}/yaml`, {
        credentials: "include",
      });
      const text = await res.text();
      if (!res.ok) {
        setError(text || `HTTP ${res.status}`);
        return;
      }
      setBody(text);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load spec");
    } finally {
      setLoading(false);
    }
  }

  function downloadYaml() {
    if (!body) return;
    const blob = new Blob([body], { type: "application/yaml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${reportId}.yaml`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  return (
    <Dialog open={open} onOpenChange={loadOnOpen}>
      <Button
        size="sm"
        variant="outline"
        onClick={() => loadOnOpen(true)}
      >
        <Code2 className="mr-2 h-3 w-3" />
        View spec
      </Button>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Report spec</DialogTitle>
          <DialogDescription>
            Canonical YAML rendering. Pipe this into{" "}
            <code className="rounded bg-muted px-1 py-0.5">
              oddish reports create --spec
            </code>{" "}
            to clone or fork the report.
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        ) : error ? (
          <pre className="rounded-md border border-red-500/40 bg-red-500/5 p-3 text-xs text-red-300">
            {error}
          </pre>
        ) : body !== null ? (
          <CodeBlock code={body} language="yaml" maxHeight="60vh" />
        ) : null}

        {body !== null ? (
          <div className="flex justify-end pt-2">
            <Button size="sm" variant="ghost" onClick={downloadYaml}>
              <Download className="mr-2 h-3 w-3" />
              Download .yaml
            </Button>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
