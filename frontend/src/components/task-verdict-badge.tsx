import {
  AlertTriangle,
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

type VerdictPresentation = {
  pending: boolean;
  failed: boolean;
  isGood: boolean | null;
  icon: ReactNode;
  title: string;
  detail: string | null;
  toneCard: string;
  toneInline: string;
};

function presentVerdict(
  task: Task,
  iconSizeClass: string
): VerdictPresentation {
  const status = task.verdict_status;
  const verdict = task.verdict ?? null;
  const verdictPending =
    status === "running" || status === "pending" || status === "queued";
  const failed = status === "failed";
  const isGood = verdict?.is_good ?? null;
  // Trial analyses run before the verdict can be synthesized; surface that
  // separately so we don't render the neutral "Verdict pending" state while
  // work is actually in flight.
  const analysesInFlight =
    !verdictPending &&
    !failed &&
    isGood == null &&
    (task.status === "analyzing" ||
      (task.trials ?? []).some((t) =>
        isActivePipelineStatus(t.analysis_status)
      ));
  const pending = verdictPending || analysesInFlight;

  let icon: ReactNode;
  let title: string;
  let toneCard: string;
  let toneInline: string;
  if (verdictPending) {
    icon = (
      <Loader2
        className={`${iconSizeClass} shrink-0 animate-spin text-blue-500`}
      />
    );
    title = "Computing verdict...";
    toneCard = "border-blue-500/30 bg-blue-500/5";
    toneInline = "border-[color:var(--paper-line)]";
  } else if (analysesInFlight) {
    icon = (
      <Loader2
        className={`${iconSizeClass} shrink-0 animate-spin text-blue-500`}
      />
    );
    title = "Analyzing trials...";
    toneCard = "border-blue-500/30 bg-blue-500/5";
    toneInline = "border-[color:var(--paper-line)]";
  } else if (failed) {
    icon = <XCircle className={`${iconSizeClass} shrink-0 text-red-500`} />;
    title = "Verdict failed";
    toneCard = "border-red-500/30 bg-red-500/5";
    toneInline = "border-red-500/40 bg-red-500/[0.04]";
  } else if (isGood === true) {
    icon = (
      <CheckCircle2 className={`${iconSizeClass} shrink-0 text-emerald-500`} />
    );
    title = "Task is good";
    toneCard = "border-emerald-500/30 bg-emerald-500/5";
    toneInline = "border-emerald-500/40 bg-emerald-500/[0.04]";
  } else if (isGood === false) {
    icon = (
      <AlertTriangle className={`${iconSizeClass} shrink-0 text-amber-500`} />
    );
    title = "Needs review";
    toneCard = "border-amber-500/30 bg-amber-500/5";
    toneInline = "border-amber-500/40 bg-amber-500/[0.04]";
  } else {
    icon = (
      <Microscope className={`${iconSizeClass} shrink-0 text-slate-500`} />
    );
    title = "Verdict pending";
    toneCard = "border-slate-500/30 bg-slate-500/5";
    toneInline = "border-[color:var(--paper-line)]";
  }

  let detail: string | null = null;
  if (failed && task.verdict_error) {
    detail = task.verdict_error;
  } else if (isGood === true) {
    detail = verdict?.reasoning?.trim() || null;
  } else if (isGood === false) {
    detail = verdict?.primary_issue ?? verdict?.reasoning ?? null;
  }

  return { pending, failed, isGood, icon, title, detail, toneCard, toneInline };
}

export function TaskVerdictBadge({
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
  const p = presentVerdict(task, iconSize);
  const verdict = task.verdict ?? null;
  const showRunButton =
    onRunJudge != null && !p.pending && !isRunning && verdict?.is_good == null;
  const showCancelButton = onCancelJudge != null && p.pending;
  const runLabel =
    task.verdict_status || task.verdict ? "Rerun judge" : "Run judge";

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
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-mono text-[12px] font-semibold text-[color:var(--paper-ink)]">
              {isRunning ? "Queuing LLM judge..." : p.title}
            </span>
            {verdict?.confidence ? (
              <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
                · {verdict.confidence} confidence
              </span>
            ) : null}
          </div>
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
            {isCancelling ? "Cancelling..." : "Cancel judge"}
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
          QA Verdict
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-3">
        <div className="flex items-start gap-3">
          {p.icon}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm font-bold">{p.title}</span>
              {verdict?.confidence ? (
                <span className="text-muted-foreground text-xs">
                  · {verdict.confidence} confidence
                </span>
              ) : null}
            </div>
            {p.detail ? (
              <p className="text-muted-foreground mt-1 text-xs">{p.detail}</p>
            ) : null}
            {verdict?.recommendations && verdict.recommendations.length > 0 ? (
              <div className="mt-2 space-y-1">
                {verdict.recommendations.map((rec, idx) => (
                  <p
                    key={idx}
                    className="text-muted-foreground/80 text-xs italic"
                  >
                    💡 {rec}
                  </p>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
