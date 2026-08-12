import {
  CheckCircle2,
  Loader2,
  Microscope,
  OctagonX,
  XCircle,
} from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { isActivePipelineStatus } from "@/lib/job-status";
import type { Task } from "@/lib/types";

type QaStatusPresentation = {
  pending: boolean;
  icon: ReactNode;
  title: string;
  detail: string | null;
  toneCard: string;
  toneInline: string;
};

/**
 * The state of the task's QA job -- not its conclusion.
 *
 * The accept/reject verdict used to render here. It is computed over every
 * trial of the task across every version (``qa_handler.run_task_qa_job``),
 * while all three badges sit next to version-scoped content: the task page has
 * a version selector, the drawer and the overview pane are pinned to one
 * version. So the label could contradict the trials listed right beside it.
 * Until the verdict knows which version it covers, only the job state is
 * shown. The payload is still stored and still drives the dashboard counts,
 * the GitHub comment, and the Slack alert.
 */
function presentQaStatus(
  task: Task,
  iconSizeClass: string
): QaStatusPresentation {
  const status = task.verdict_status;
  const jobPending =
    status === "running" || status === "pending" || status === "queued";
  const failed = status === "failed";
  const done = status === "success";
  // The single task-level QA job classifies every trial and then synthesizes
  // the verdict, so any in-flight classification is also "QA running".
  const analysesInFlight =
    !jobPending &&
    !failed &&
    !done &&
    (task.status === "analyzing" ||
      (task.trials ?? []).some((t) =>
        isActivePipelineStatus(t.analysis_status)
      ));
  const pending = jobPending || analysesInFlight;

  let icon: ReactNode;
  let title: string;
  let toneCard: string;
  let toneInline: string;
  if (pending) {
    icon = (
      <Loader2
        className={`${iconSizeClass} shrink-0 animate-spin text-blue-500`}
      />
    );
    title = "Running QA...";
    toneCard = "border-blue-500/30 bg-blue-500/5";
    toneInline = "border-[color:var(--paper-line)]";
  } else if (failed) {
    icon = <XCircle className={`${iconSizeClass} shrink-0 text-red-500`} />;
    title = "QA failed";
    toneCard = "border-red-500/30 bg-red-500/5";
    toneInline = "border-red-500/40 bg-red-500/[0.04]";
  } else if (done) {
    // Deliberately not green: this says the job finished, not that the task
    // passed. A colour that reads as approval is the thing being removed.
    icon = (
      <CheckCircle2 className={`${iconSizeClass} shrink-0 text-slate-500`} />
    );
    title = "QA complete";
    toneCard = "border-slate-500/30 bg-slate-500/5";
    toneInline = "border-[color:var(--paper-line)]";
  } else {
    icon = (
      <Microscope className={`${iconSizeClass} shrink-0 text-slate-500`} />
    );
    title = "QA pending";
    toneCard = "border-slate-500/30 bg-slate-500/5";
    toneInline = "border-[color:var(--paper-line)]";
  }

  // A failed job's error is the only text left: it describes the run, not the
  // task, so no version scoping question arises.
  const detail = failed ? (task.verdict_error ?? null) : null;

  return { pending, icon, title, detail, toneCard, toneInline };
}

export function TaskQaStatusBadge({
  task,
  variant,
  onRunJudge,
  onCancelJudge,
  isRunning,
  isCancelling,
  error,
}: {
  task: Task;
  variant: "card" | "inline";
  onRunJudge?: () => void;
  onCancelJudge?: () => void;
  isRunning?: boolean;
  isCancelling?: boolean;
  error?: string | null;
}) {
  const hasAny =
    Boolean(task.run_analysis) ||
    Boolean(task.verdict_status) ||
    Boolean(task.verdict);
  if (!hasAny && !onRunJudge) return null;

  const iconSize = variant === "card" ? "h-5 w-5 mt-0.5" : "h-4 w-4";
  const p = presentQaStatus(task, iconSize);
  const showRunButton = onRunJudge != null && !p.pending && !isRunning;
  const showCancelButton = onCancelJudge != null && p.pending;
  const runLabel = task.verdict_status || task.verdict ? "Rerun QA" : "Run QA";

  if (variant === "inline") {
    return (
      <div
        className={`flex items-start gap-2.5 rounded-[10px] border px-3 py-2 ${p.toneInline}`}
      >
        {isRunning ? (
          <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-500" />
        ) : (
          p.icon
        )}
        <div className="min-w-0 flex-1">
          <span className="font-mono text-[12px] font-semibold text-[color:var(--paper-ink)]">
            {isRunning ? "Queuing QA..." : p.title}
          </span>
          {p.detail ? (
            <p className="mt-0.5 font-mono text-[11px] leading-snug text-[color:var(--paper-ink-2)]">
              {p.detail}
            </p>
          ) : null}
          {error ? (
            <p className="mt-0.5 font-mono text-[11px] leading-snug text-red-500">
              {error}
            </p>
          ) : null}
        </div>
        {showCancelButton ? (
          <Button
            type="button"
            variant="destructive"
            onClick={onCancelJudge}
            disabled={isCancelling}
            className="h-7 shrink-0 rounded-[7px] px-3 font-mono text-[11px]"
          >
            {isCancelling ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <OctagonX className="mr-1 h-3.5 w-3.5" />
            )}
            {isCancelling ? "Cancelling..." : "Cancel QA"}
          </Button>
        ) : showRunButton ? (
          <Button
            type="button"
            variant="outline"
            onClick={onRunJudge}
            disabled={isRunning}
            className="h-7 shrink-0 rounded-[7px] px-3 font-mono text-[11px]"
          >
            {runLabel}
          </Button>
        ) : null}
      </div>
    );
  }

  return (
    <Card className={p.toneCard}>
      <CardHeader className="px-4 pt-2 pb-1">
        <CardTitle className="text-muted-foreground flex items-center gap-1.5 text-[11px] font-semibold tracking-wider uppercase">
          <Microscope className="h-3 w-3" />
          QA
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-3">
        <div className="flex items-start gap-3">
          {p.icon}
          <div className="min-w-0 flex-1">
            <span className="font-mono text-sm font-bold">{p.title}</span>
            {p.detail ? (
              <p className="text-muted-foreground mt-1 text-sm">{p.detail}</p>
            ) : null}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
