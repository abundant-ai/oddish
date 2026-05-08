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

// Browsers expose a dropped folder as a 0-byte File with no extension.
// Catch that case (and any other obvious non-zip) before the upload so
// the user sees a clear "zip the folder first" message instead of a
// generic backend rejection at the end of a megabyte-scale upload.
function looksLikeZip(file: File): boolean {
  if (file.size === 0) return false;
  const name = file.name.toLowerCase();
  return name.endsWith(".zip") || file.type === "application/zip";
}

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

  const invalid = file !== null && !looksLikeZip(file);

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
          invalid
            ? "border-rose-500/60 bg-rose-500/5"
            : hover
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
              {(file.size / (1024 * 1024)).toFixed(2)} MiB
            </span>
            {invalid ? (
              <span className="text-rose-500">
                That looks like a folder, not a .zip — run{" "}
                <code className="font-mono">zip -r my.zip my/</code> first.
              </span>
            ) : null}
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
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportResponse | null>(null);

  function reset() {
    setTaskZip(null);
    setRunZip(null);
    setTaskId("");
    setExperiment("");
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

  const taskZipValid = taskZip === null || looksLikeZip(taskZip);
  const runZipValid = runZip === null || looksLikeZip(runZip);
  const hasZip = taskZip !== null || runZip !== null;

  // The backend infers the target task from the run zip's job-dir
  // name and accepts task ID *or* name, so we no longer require the
  // user to fill the field. We surface the backend's error message
  // when inference doesn't find a match.
  const canSubmit = !submitting && hasZip && taskZipValid && runZipValid;

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
          <DialogTitle>Import from .zip</DialogTitle>
          <DialogDescription>
            Drop a Harbor run zip; the target task is inferred from the
            job-dir name. Same outcome as{" "}
            <code className="font-mono">oddish upload</code>.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <ResultPanel result={result} />
        ) : (
          <div className="space-y-4">
            <DropSlot
              label="Run / jobs zip"
              hint="A Harbor job dir (with result.json), zipped"
              file={runZip}
              onChange={setRunZip}
              disabled={submitting}
            />

            {runZip ? (
              <>
                <div className="space-y-1.5">
                  <Label htmlFor="import-task-id" className="text-xs">
                    Target task{" "}
                    {taskZip ? (
                      <span className="text-muted-foreground">
                        (uploaded from task zip)
                      </span>
                    ) : (
                      <span className="text-muted-foreground">
                        (ID or name; auto-detected if blank)
                      </span>
                    )}
                  </Label>
                  <Input
                    id="import-task-id"
                    value={taskZip ? `→ ${taskZip.name}` : taskId}
                    onChange={(event) => setTaskId(event.target.value)}
                    placeholder="Leave blank to use the run zip's task name"
                    disabled={submitting || taskZip !== null}
                    className="h-8"
                  />
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="import-experiment" className="text-xs">
                    Experiment name{" "}
                    <span className="text-muted-foreground">(optional)</span>
                  </Label>
                  <Input
                    id="import-experiment"
                    value={experiment}
                    onChange={(event) => setExperiment(event.target.value)}
                    placeholder="Auto-generated if blank"
                    disabled={submitting}
                    className="h-8"
                  />
                </div>
              </>
            ) : null}

            <details className="text-xs">
              <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
                Don&apos;t have the task in oddish yet? Upload task files too.
              </summary>
              <div className="mt-2">
                <DropSlot
                  label="Task zip"
                  hint="A Harbor task dir (task.toml + environment/ + tests/), zipped"
                  file={taskZip}
                  onChange={setTaskZip}
                  disabled={submitting}
                />
              </div>
            </details>

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
