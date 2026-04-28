"use client";

import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Alert,
  AlertDescription,
  AlertTitle,
} from "@/components/ui/alert";
import { Loader2, Upload } from "lucide-react";

type ImportTrial = {
  job_name: string;
  trial_name: string;
  trial_id: string | null;
  status: "imported" | "error";
  error: string | null;
  files_extracted: number;
};

type ImportResponse = {
  task: {
    task_id: string;
    name: string;
    version: number | null;
    existing_task: boolean;
    content_unchanged: boolean;
  } | null;
  experiment_id: string | null;
  experiment_name: string | null;
  trials: ImportTrial[];
  trial_count: number;
  trials_imported: number;
  trials_failed: number;
};

// Native drag-drop slot. Picks a single .zip file; clicking opens a
// file picker as a keyboard-accessible fallback.
function DropSlot({
  label,
  hint,
  file,
  onChange,
  disabled,
}: {
  label: string;
  hint: string;
  file: File | null;
  onChange: (next: File | null) => void;
  disabled?: boolean;
}) {
  const [hover, setHover] = useState(false);

  function handleDrop(event: React.DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setHover(false);
    if (disabled) return;
    const dropped = event.dataTransfer.files?.[0];
    if (dropped) onChange(dropped);
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between">
        <Label className="text-xs uppercase tracking-wide text-muted-foreground">
          {label}
        </Label>
        {file ? (
          <button
            type="button"
            className="text-[11px] text-muted-foreground hover:text-foreground"
            onClick={() => onChange(null)}
            disabled={disabled}
          >
            Clear
          </button>
        ) : null}
      </div>
      <label
        className={`flex cursor-pointer flex-col items-center justify-center gap-1 rounded-md border border-dashed px-4 py-6 text-center text-xs transition-colors ${
          hover
            ? "border-[#6f88b4] bg-[#6f88b4]/5"
            : "border-border/70 bg-muted/30 hover:border-border"
        } ${disabled ? "pointer-events-none opacity-60" : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setHover(true);
        }}
        onDragLeave={() => setHover(false)}
        onDrop={handleDrop}
      >
        <input
          type="file"
          accept=".zip,application/zip"
          className="hidden"
          disabled={disabled}
          onChange={(event) => {
            const picked = event.target.files?.[0] ?? null;
            onChange(picked);
            // Clear the input so picking the same file twice still fires.
            event.target.value = "";
          }}
        />
        {file ? (
          <>
            <span className="font-medium text-foreground">{file.name}</span>
            <span className="text-muted-foreground">
              {(file.size / (1024 * 1024)).toFixed(1)} MiB
            </span>
          </>
        ) : (
          <>
            <span className="text-foreground">Drop a .zip here</span>
            <span className="text-muted-foreground">{hint}</span>
          </>
        )}
      </label>
    </div>
  );
}

export function ImportDialog({ onImported }: { onImported?: () => void }) {
  const [open, setOpen] = useState(false);
  const [taskZip, setTaskZip] = useState<File | null>(null);
  const [runZip, setRunZip] = useState<File | null>(null);
  const [taskId, setTaskId] = useState("");
  const [experiment, setExperiment] = useState("");
  const [skipArtifacts, setSkipArtifacts] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResponse | null>(null);

  function reset() {
    setTaskZip(null);
    setRunZip(null);
    setTaskId("");
    setExperiment("");
    setSkipArtifacts(false);
    setSubmitting(false);
    setError(null);
    setResult(null);
  }

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (!next) {
      // Defer reset so the closing animation doesn't flicker the
      // success state away before the user reads it.
      setTimeout(reset, 200);
    }
  }

  const canSubmit =
    !submitting &&
    (taskZip !== null || runZip !== null) &&
    // Run-only imports must have a target task ID; otherwise the
    // server returns 400 and we'd waste the upload.
    (taskZip !== null || runZip === null || taskId.trim().length > 0);

  async function handleSubmit() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    setResult(null);

    const form = new FormData();
    if (taskZip) form.append("task_zip", taskZip);
    if (runZip) form.append("run_zip", runZip);
    if (taskId.trim()) form.append("task_id", taskId.trim());
    if (experiment.trim()) form.append("experiment", experiment.trim());
    if (skipArtifacts) form.append("skip_artifacts", "true");

    try {
      const res = await fetch("/api/imports/zip", {
        method: "POST",
        credentials: "include",
        body: form,
      });
      const data: ImportResponse | { error?: string; details?: string } =
        await res.json().catch(() => ({}) as Record<string, never>);
      if (!res.ok) {
        const message =
          ("error" in data && data.error) ||
          ("details" in data && data.details) ||
          res.statusText ||
          "Import failed";
        throw new Error(String(message));
      }
      setResult(data as ImportResponse);
      onImported?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-8 px-3 text-[11px]"
        >
          <Upload className="mr-1 h-3.5 w-3.5" />
          Import
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Import task or trials</DialogTitle>
          <DialogDescription>
            Drop a Harbor task zip, a Harbor run/jobs zip, or both. Same
            outcome as <code className="font-mono">oddish upload</code> —
            see DOCS for the CLI equivalents.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <ResultPanel result={result} />
        ) : (
          <div className="space-y-4">
            <DropSlot
              label="Task files (optional)"
              hint="A Harbor task dir: task.toml, instruction.md, environment/, tests/"
              file={taskZip}
              onChange={setTaskZip}
              disabled={submitting}
            />
            <DropSlot
              label="Harbor run / jobs (optional)"
              hint="A job dir with result.json, or a parent dir of job dirs"
              file={runZip}
              onChange={setRunZip}
              disabled={submitting}
            />

            {runZip && !taskZip ? (
              <div className="space-y-1.5">
                <Label htmlFor="import-task-id" className="text-xs">
                  Target task ID
                </Label>
                <Input
                  id="import-task-id"
                  value={taskId}
                  onChange={(event) => setTaskId(event.target.value)}
                  placeholder="task_abcdef12 (run-only imports require an existing task)"
                  disabled={submitting}
                  className="h-8"
                />
              </div>
            ) : null}

            {runZip ? (
              <>
                <div className="space-y-1.5">
                  <Label htmlFor="import-experiment" className="text-xs">
                    Experiment name (optional)
                  </Label>
                  <Input
                    id="import-experiment"
                    value={experiment}
                    onChange={(event) => setExperiment(event.target.value)}
                    placeholder="Leave blank to auto-generate"
                    disabled={submitting}
                    className="h-8"
                  />
                </div>
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={skipArtifacts}
                    onChange={(event) => setSkipArtifacts(event.target.checked)}
                    disabled={submitting}
                  />
                  Skip artifacts (register metadata only — same as{" "}
                  <code className="font-mono">--skip-artifacts</code>)
                </label>
              </>
            ) : null}

            {error ? (
              <Alert variant="destructive">
                <AlertTitle>Import failed</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            ) : null}
          </div>
        )}

        <DialogFooter>
          {result ? (
            <Button type="button" onClick={() => handleOpenChange(false)}>
              Close
            </Button>
          ) : (
            <>
              <Button
                type="button"
                variant="outline"
                onClick={() => handleOpenChange(false)}
                disabled={submitting}
              >
                Cancel
              </Button>
              <Button
                type="button"
                onClick={handleSubmit}
                disabled={!canSubmit}
              >
                {submitting ? (
                  <>
                    <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    Importing
                  </>
                ) : (
                  "Import"
                )}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ResultPanel({ result }: { result: ImportResponse }) {
  const taskLine = result.task
    ? result.task.content_unchanged
      ? `Task ${result.task.name} unchanged (version ${result.task.version}).`
      : result.task.existing_task
        ? `Task ${result.task.name} updated to version ${result.task.version}.`
        : `Task ${result.task.name} uploaded as version ${result.task.version}.`
    : null;

  return (
    <div className="space-y-3 text-sm">
      {taskLine ? (
        <div className="rounded-md border border-border/60 bg-muted/30 px-3 py-2">
          {taskLine}
          {result.task ? (
            <div className="mt-0.5 font-mono text-xs text-muted-foreground">
              {result.task.task_id}
            </div>
          ) : null}
        </div>
      ) : null}

      {result.trial_count > 0 ? (
        <div className="rounded-md border border-border/60 bg-muted/30 px-3 py-2">
          <div>
            Imported{" "}
            <span className="font-medium">{result.trials_imported}</span> of{" "}
            {result.trial_count} trial(s)
            {result.trials_failed > 0
              ? `, ${result.trials_failed} failed`
              : null}
            .
          </div>
          {result.experiment_id ? (
            <div className="mt-1">
              <a
                href={`/experiments/${encodeURIComponent(result.experiment_id)}`}
                className="text-[#5d77a5] hover:underline dark:text-[#a8b8d2]"
              >
                Open experiment{" "}
                {result.experiment_name
                  ? `"${result.experiment_name}"`
                  : result.experiment_id}
              </a>
            </div>
          ) : null}
        </div>
      ) : null}

      {result.trials_failed > 0 ? (
        <Alert variant="destructive">
          <AlertTitle>Some trials failed</AlertTitle>
          <AlertDescription>
            <ul className="mt-1 space-y-0.5 text-xs">
              {result.trials
                .filter((t) => t.status === "error")
                .slice(0, 5)
                .map((t) => (
                  <li key={`${t.job_name}/${t.trial_name}`}>
                    <span className="font-mono">
                      {t.job_name}/{t.trial_name}
                    </span>
                    : {t.error ?? "unknown error"}
                  </li>
                ))}
              {result.trials_failed > 5 ? (
                <li className="text-muted-foreground">
                  + {result.trials_failed - 5} more
                </li>
              ) : null}
            </ul>
          </AlertDescription>
        </Alert>
      ) : null}
    </div>
  );
}
