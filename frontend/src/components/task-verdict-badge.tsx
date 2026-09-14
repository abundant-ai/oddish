import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Microscope,
  OctagonX,
} from "lucide-react";
import type { ReactNode } from "react";

import { AnalysisProse } from "@/components/analysis-prose";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { VERDICT_LABELS, taskReviewStatus } from "@/lib/review";
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
  iconSizeClass: string,
  qaActive: boolean,
  mustFixCount = task.must_fix_count ?? 0
): VerdictPresentation {
  const status = task.verdict_status;
  const verdict = task.verdict ?? null;
  const review = qaActive ? "running" : taskReviewStatus(task);
  const pending = review === "queued" || review === "running";
  const failed = review === "error";
  const isGood =
    review === "accepted" ? true : review === "needs_fixes" ? false : null;

  let icon: ReactNode;
  let title: string;
  let toneCard: string;
  let toneInline: string;
  if (mustFixCount > 0) {
    icon = (
      <AlertTriangle className={`${iconSizeClass} shrink-0 text-red-600`} />
    );
    title = `${mustFixCount} Must fix`;
    toneCard = "border-red-500/50 bg-red-500/10";
    toneInline = "border-red-500/50 bg-red-500/10";
  } else if (pending) {
    icon = (
      <Loader2
        className={`${iconSizeClass} shrink-0 animate-spin text-blue-500`}
      />
    );
    title = VERDICT_LABELS[review];
    toneCard = "border-blue-500/30 bg-blue-500/5";
    toneInline = "border-[color:var(--paper-line)]";
  } else if (failed) {
    icon = (
      <AlertTriangle className={`${iconSizeClass} shrink-0 text-amber-600`} />
    );
    title = VERDICT_LABELS.error;
    toneCard = "border-amber-500/30 bg-amber-500/5";
    toneInline = "border-amber-500/40 bg-amber-500/[0.04]";
  } else if (review === "outdated") {
    icon = (
      <Microscope
        className={`${iconSizeClass} text-muted-foreground shrink-0`}
      />
    );
    title = VERDICT_LABELS.outdated;
    toneCard = "border-border";
    toneInline = "border-border";
  } else if (isGood === true) {
    icon = (
      <CheckCircle2 className={`${iconSizeClass} shrink-0 text-emerald-500`} />
    );
    title = VERDICT_LABELS.accepted;
    toneCard = "border-emerald-500/30 bg-emerald-500/5";
    toneInline = "border-emerald-500/40 bg-emerald-500/[0.04]";
  } else if (isGood === false) {
    icon = (
      <AlertTriangle className={`${iconSizeClass} shrink-0 text-red-600`} />
    );
    title = VERDICT_LABELS.needs_fixes;
    toneCard = "border-red-500/50 bg-red-500/10";
    toneInline = "border-red-500/50 bg-red-500/10";
  } else {
    icon = (
      <Microscope className={`${iconSizeClass} shrink-0 text-slate-500`} />
    );
    title = status === "success" ? "No overall result" : VERDICT_LABELS.never;
    toneCard = "border-slate-500/30 bg-slate-500/5";
    toneInline = "border-[color:var(--paper-line)]";
  }

  // An in-flight review must never display a previous verdict from cached data.
  let detail: string | null = null;
  if (failed) {
    detail = task.verdict_error ?? null;
  } else if (!pending && isGood === true) {
    detail = verdict?.reasoning?.trim() || null;
  } else if (!pending && isGood === false) {
    detail = verdict?.primary_issue ?? verdict?.reasoning ?? null;
  }

  return { pending, failed, isGood, icon, title, detail, toneCard, toneInline };
}

export function TaskVerdictBadge({
  task,
  variant,
  rejectionSource,
  onViewFindings,
  onRunJudge,
  onCancelJudge,
  isRunning,
  qaActive = false,
  isCancelling,
  error,
  mustFixCount = task.must_fix_count ?? 0,
}: {
  task: Task;
  variant: "card" | "inline" | "summary";
  rejectionSource?: "Pre-trial audit" | "Run review";
  onViewFindings?: () => void;
  onRunJudge?: () => void;
  onCancelJudge?: () => void;
  isRunning?: boolean;
  qaActive?: boolean;
  isCancelling?: boolean;
  error?: string | null;
  /** Required findings for the selected version, including run reviews. */
  mustFixCount?: number;
}) {
  const hasAny =
    mustFixCount > 0 ||
    qaActive ||
    Boolean(task.run_analysis) ||
    Boolean(task.verdict_status) ||
    Boolean(task.verdict);
  if (!hasAny && !onRunJudge) return null;

  const iconSize = variant === "card" ? "h-5 w-5 mt-0.5" : "h-4 w-4";
  const p = presentVerdict(task, iconSize, qaActive, mustFixCount);
  const shownDetail = mustFixCount > 0 || p.isGood === false ? null : p.detail;
  const verdict = task.verdict ?? null;
  const showRunButton = onRunJudge != null && !p.pending && !isRunning;
  const showCancelButton = onCancelJudge != null && p.pending;
  const runLabel = `Review runs${task.current_version != null ? ` for v${task.current_version}` : ""}`;

  if (variant === "inline" || variant === "summary") {
    return (
      <div
        className={`flex flex-wrap items-start gap-2.5 rounded-[10px] border px-3 py-2 ${p.toneInline}`}
      >
        {isRunning ? (
          <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-500" />
        ) : (
          p.icon
        )}
        <div className="min-w-0 flex-1 basis-48">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span
              className={
                variant === "summary"
                  ? "text-base font-semibold"
                  : "font-mono text-[12px] font-semibold text-[color:var(--paper-ink)]"
              }
            >
              {variant === "summary" && mustFixCount > 0 && rejectionSource
                ? `Rejected · ${rejectionSource}`
                : isRunning && mustFixCount === 0
                  ? "Queuing review…"
                  : p.title}
            </span>
            {mustFixCount === 0 && p.isGood !== null && verdict?.confidence ? (
              <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
                · {verdict.confidence} confidence
              </span>
            ) : null}
          </div>
          {variant === "summary" && mustFixCount > 0 && rejectionSource ? (
            <p className="mt-1 text-sm">{mustFixCount} Must fix</p>
          ) : null}
          {shownDetail ? (
            <p
              className={
                variant === "summary"
                  ? "mt-1 line-clamp-2 text-sm"
                  : "mt-0.5 font-mono text-[11px] leading-snug text-[color:var(--paper-ink-2)]"
              }
            >
              {shownDetail}
            </p>
          ) : null}
          {/* A rejected task's fixes are the actionable half of the verdict.
              They rendered only in the card variant, so the panes that moved
              from the pinned card to this badge kept the rejection and lost
              what to do about it. */}
          {mustFixCount === 0 &&
          variant !== "summary" &&
          p.isGood !== null &&
          verdict?.recommendations &&
          verdict.recommendations.length > 0 ? (
            <div className="mt-1.5 border-l-2 border-amber-500/50 pl-2">
              <span className="font-mono text-[10px] font-semibold tracking-wider text-[color:var(--paper-ink-3)] uppercase">
                Fixes ({verdict.recommendations.length})
              </span>
              <div className="mt-0.5 space-y-0.5">
                {verdict.recommendations.map((rec, idx) => (
                  <AnalysisProse
                    key={idx}
                    text={rec}
                    className="font-mono text-[11px] leading-snug text-[color:var(--paper-ink-2)]"
                  />
                ))}
              </div>
            </div>
          ) : null}
          {error ? (
            <p className="mt-0.5 font-mono text-[11px] leading-snug text-red-500">
              {error}
            </p>
          ) : null}
        </div>
        {variant === "summary" && onViewFindings && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onViewFindings}
          >
            View findings
          </Button>
        )}
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
          Run reviews
        </CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-3">
        <div className="flex items-start gap-3">
          {p.icon}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm font-bold">{p.title}</span>
              {mustFixCount === 0 &&
              p.isGood !== null &&
              verdict?.confidence ? (
                <span className="text-muted-foreground text-xs">
                  · {verdict.confidence} confidence
                </span>
              ) : null}
            </div>
            {shownDetail ? (
              <AnalysisProse
                text={shownDetail}
                className="text-muted-foreground mt-1"
              />
            ) : null}
            {mustFixCount === 0 &&
            p.isGood !== null &&
            verdict?.recommendations &&
            verdict.recommendations.length > 0 ? (
              <div className="border-border/60 bg-muted/30 mt-2 rounded-md border border-l-2 border-l-amber-500/60 p-2.5">
                <span className="text-foreground/80 font-mono text-[10px] font-semibold tracking-wider uppercase">
                  Fixes ({verdict.recommendations.length})
                </span>
                <div className="mt-1 space-y-1">
                  {verdict.recommendations.map((rec, idx) => (
                    <AnalysisProse
                      key={idx}
                      text={rec}
                      className="text-muted-foreground"
                    />
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
