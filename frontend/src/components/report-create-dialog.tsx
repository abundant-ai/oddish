"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { mutate } from "swr";

const STARTER_YAML = `name: "My report"
description: "Short blurb of what this report compares."
agents:
  - id: claude-sonnet
    agent: claude-code
    model: anthropic/claude-sonnet-4-5
  - id: gpt-5
    agent: codex
    model: openai/gpt-5.2
task_versions:
  # environment is a per-task infra requirement (GPU, base image,
  # network) — agents can run anywhere; tasks can't. Set it per row.
  - task_id: <paste-task-id-here>      # version defaults to current
    environment: modal
  - task_version_id: <or-paste-task_version_id-here>
    environment: docker
trials_per_cell: 3
selection:
  strategy: latest                       # latest | best_reward | first | random
backfill:
  enabled: true
`;

interface CreatedReport {
  id: string;
  name?: string;
}

interface ApiError {
  detail?: unknown;
  error?: string;
}

/**
 * "Create report" button + dialog. The textarea body is sent verbatim
 * to the backend ``POST /reports.yaml`` route, which parses + validates
 * server-side and returns the canonical error message on failure.
 */
export function ReportCreateDialog() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState(STARTER_YAML);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/api/reports.yaml", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/yaml" },
        body,
      });
      const text = await res.text();
      let parsed: ApiError | CreatedReport | null = null;
      try {
        parsed = text ? JSON.parse(text) : null;
      } catch {
        parsed = { error: text };
      }
      if (!res.ok) {
        const detail =
          (parsed as ApiError)?.detail ?? (parsed as ApiError)?.error ?? text;
        setError(
          typeof detail === "string"
            ? detail
            : JSON.stringify(detail, null, 2),
        );
        return;
      }
      const report = parsed as CreatedReport;
      setOpen(false);
      void mutate("/api/reports?limit=100");
      if (report.id) {
        router.push(`/reports/${report.id}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create report");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button size="sm" onClick={() => setOpen(true)}>
        <Plus className="mr-2 h-3 w-3" />
        Create report
      </Button>

      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Create report</DialogTitle>
          <DialogDescription>
            Paste a YAML spec. The server validates it; errors here mirror{" "}
            <code className="rounded bg-muted px-1 py-0.5">
              oddish reports create --spec
            </code>
            . Use{" "}
            <code className="rounded bg-muted px-1 py-0.5">task_id</code> to
            target a task&apos;s current version, or{" "}
            <code className="rounded bg-muted px-1 py-0.5">
              task_version_id
            </code>{" "}
            to pin a specific one.
          </DialogDescription>
        </DialogHeader>

        <textarea
          spellCheck={false}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          className="h-[420px] w-full rounded-md border bg-background px-3 py-2 font-mono text-xs"
        />

        {error ? (
          <pre className="max-h-40 overflow-auto rounded-md border border-red-500/40 bg-red-500/5 p-3 text-xs text-red-300">
            {error}
          </pre>
        ) : null}

        <DialogFooter>
          <Button
            variant="ghost"
            disabled={submitting}
            onClick={() => setOpen(false)}
          >
            Cancel
          </Button>
          <Button disabled={submitting || !body.trim()} onClick={submit}>
            {submitting ? (
              <>
                <Loader2 className="mr-2 h-3 w-3 animate-spin" /> Creating…
              </>
            ) : (
              "Create"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
