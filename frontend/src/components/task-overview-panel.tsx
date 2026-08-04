"use client";

import { useMemo } from "react";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { ArrowUpRight, Loader2, SearchCode } from "lucide-react";

import { cn } from "@/lib/utils";
import { fetcher } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { AnalysisProse } from "@/components/analysis-prose";
import { SeverityGroups } from "@/components/qa-report/action-items";
import { CopyJsonButton } from "@/components/qa-report/copy-json-button";
import { FALLBACK_TOKEN, VERDICT_TOKENS } from "@/components/qa-report/tokens";
import { TaskVerdictBadge } from "@/components/task-verdict-badge";
import { isActivePipelineStatus } from "@/lib/job-status";
import { formatCostUsd, hasDisplayableCostUsd } from "@/lib/format";
import type {
  AnalysisClassification,
  PreTrialFinding,
  Task,
  Trial,
} from "@/lib/types";

export type StaticCheckState =
  | "unaudited"
  | "running"
  | "failed"
  | "clean"
  | "findings";

/**
 * What to say for a task's source audit. Empty findings mean three
 * different things depending on status: only `success` with no items is
 * genuinely "we looked and found nothing".
 */
export function staticCheckState(
  status: string | null | undefined,
  findingCount: number,
): StaticCheckState {
  if (!status) return "unaudited";
  const normalized = status.toLowerCase();
  if (normalized === "running" || normalized === "queued") return "running";
  if (normalized === "success") return findingCount > 0 ? "findings" : "clean";
  return "failed";
}

/** Problems first: a cheated success or broken task outranks a clean run. */
const CLASSIFICATION_ORDER: AnalysisClassification[] = [
  "BAD_SUCCESS",
  "BAD_FAILURE",
  "HARNESS_ERROR",
  "GOOD_FAILURE",
  "GOOD_SUCCESS",
];

const CLASSIFICATION_LABELS: Record<AnalysisClassification, string> = {
  BAD_SUCCESS: "Bad success",
  BAD_FAILURE: "Bad failure",
  HARNESS_ERROR: "Harness error",
  GOOD_FAILURE: "Good failure",
  GOOD_SUCCESS: "Good success",
};

/** A finding plus where it came from: the source audit, trial QA, or both. */
interface SourcedFinding extends PreTrialFinding {
  fromAudit: boolean;
  trials: Trial[];
}

function findingKey(item: PreTrialFinding): string {
  // The server stamps content-hash ids on findings, so the same defect
  // reported by the audit and by trials collapses into one row. Items
  // without an id fall back to a content key.
  return (
    item.id ?? `${item.tier ?? ""}|${item.title ?? ""}|${item.file ?? ""}`
  );
}

function classificationRank(trial: Trial): number {
  const classification = trial.analysis?.classification;
  if (!classification) return CLASSIFICATION_ORDER.length;
  const rank = CLASSIFICATION_ORDER.indexOf(classification);
  return rank >= 0 ? rank : CLASSIFICATION_ORDER.length;
}

function trialLabel(trial: Trial): string {
  const model = trial.model?.split("/").pop();
  return model ? `${trial.agent} · ${model}` : trial.agent;
}

/**
 * The task overview: the task's own QA (verdict + the source-audit findings)
 * merged with the trial-level QA aggregated across the shown version's
 * trials, each finding and classification linking back to the trial that
 * surfaced it.
 */
export function TaskOverviewPanel({
  taskId,
  apiBaseUrl = "/api",
  version,
  verdictTask,
  checksFindings,
  checksStatus,
  checksError,
  checksCostUsd,
  onRerunChecks,
  checksRerunning,
  checksQueueError,
  checksLoading,
  checksLoadError,
  qaActive,
  onOpenTrial,
  className,
}: {
  taskId: string | null;
  apiBaseUrl?: string;
  /** Version the pane is scoped to; null aggregates every trial. */
  version?: number | null;
  /** Render the QA verdict inline — for panes whose host shows no verdict
   *  card of its own (the side-by-side "Task definition" pane). */
  verdictTask?: Task | null;
  checksFindings?: PreTrialFinding[] | null;
  checksStatus?: string | null;
  checksError?: string | null;
  checksCostUsd?: number | null;
  onRerunChecks: () => void;
  checksRerunning: boolean;
  checksQueueError?: string | null;
  /** The checks state is still being fetched: an absent status must not
   * read as "unaudited" — a Run click on that misread wipes real findings. */
  checksLoading?: boolean;
  checksLoadError?: string | null;
  /** Task-level QA in flight — keeps the trial list polling until it lands. */
  qaActive?: boolean;
  /**
   * Open a trial in the caller's own context (drawer / panel). Returns
   * false when the trial isn't addressable there; the panel then falls
   * back to the task page deep link.
   */
  onOpenTrial?: (trial: Trial) => boolean;
  className?: string;
}) {
  const router = useRouter();
  // Probes are excluded at the query: they are internal instruction-overlay
  // runs, not attempts, and their `analysis` is a different shape entirely.
  const trialsKey = taskId
    ? `${apiBaseUrl}/tasks/${taskId}/trials?probe=false`
    : null;
  const {
    data: trials,
    error: trialsError,
    isLoading: trialsLoading,
  } = useSWR<Trial[]>(trialsKey, fetcher, {
    refreshInterval: (data) => {
      const anyAnalysisLive = (data ?? []).some((trial) =>
        isActivePipelineStatus(trial.analysis_status),
      );
      return anyAnalysisLive || qaActive ? 5000 : 0;
    },
  });

  const versionTrials = useMemo(() => {
    const all = trials ?? [];
    if (version == null) return all;
    return all.filter((trial) => trial.task_version === version);
  }, [trials, version]);

  const {
    classificationCounts,
    unanalyzedCount,
    analyzedCount,
    mergedFindings,
    qaTrials,
  } = useMemo(() => {
    const byKey = new Map<string, SourcedFinding>();
    for (const item of checksFindings ?? []) {
      const key = findingKey(item);
      byKey.set(key, { ...item, id: key, fromAudit: true, trials: [] });
    }
    const counts = new Map<AnalysisClassification, number>();
    const withQa: Trial[] = [];
    let unanalyzed = 0;
    for (const trial of versionTrials) {
      if (!trial.analysis && !trial.analysis_status) {
        unanalyzed += 1;
        continue;
      }
      withQa.push(trial);
      const analysis = trial.analysis;
      if (!analysis) continue;
      counts.set(
        analysis.classification,
        (counts.get(analysis.classification) ?? 0) + 1,
      );
      for (const item of analysis.action_items ?? []) {
        const key = findingKey(item);
        const existing = byKey.get(key);
        if (existing) {
          existing.trials.push(trial);
          // One exploiting trial marks the finding exploited.
          if (item.exploited) existing.exploited = true;
        } else {
          byKey.set(key, {
            ...item,
            id: key,
            fromAudit: false,
            trials: [trial],
          });
        }
      }
    }
    withQa.sort(
      (a, b) =>
        classificationRank(a) - classificationRank(b) ||
        a.created_at.localeCompare(b.created_at),
    );
    return {
      classificationCounts: counts,
      unanalyzedCount: unanalyzed,
      analyzedCount: withQa.filter((trial) => trial.analysis).length,
      mergedFindings: Array.from(byKey.values()),
      qaTrials: withQa,
    };
  }, [versionTrials, checksFindings]);

  const openTrial = (trial: Trial) => {
    if (onOpenTrial?.(trial)) return;
    if (!taskId) return;
    const params = new URLSearchParams();
    if (trial.task_version_id) params.set("version", trial.task_version_id);
    params.set("trial", trial.id);
    router.push(`/tasks/${taskId}?${params.toString()}`);
  };

  const renderFindingSources = (item: PreTrialFinding) => {
    const sourced = item as SourcedFinding;
    if (!sourced.fromAudit && !(sourced.trials?.length > 0)) return null;
    return (
      <div className="mt-1 flex flex-wrap items-center gap-1.5">
        <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
          SEEN IN
        </span>
        {sourced.fromAudit ? (
          <span
            className="border-border text-muted-foreground inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]"
            title="Found by the pre-trial audit of the task's source"
          >
            <SearchCode className="h-3 w-3 shrink-0" aria-hidden="true" />
            Source audit
          </span>
        ) : null}
        {(sourced.trials ?? []).map((trial) => (
          <button
            key={trial.id}
            type="button"
            onClick={() => openTrial(trial)}
            className="border-border text-muted-foreground hover:text-foreground hover:border-foreground/40 inline-flex max-w-full items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors"
            title={`Open trial ${trial.name}`}
          >
            <span className="truncate">{trialLabel(trial)}</span>
            <ArrowUpRight className="h-3 w-3 shrink-0" aria-hidden="true" />
          </button>
        ))}
      </div>
    );
  };

  const checkState = staticCheckState(checksStatus, checksFindings?.length ?? 0);
  const checksStateUnknown = Boolean(checksLoading || checksLoadError);
  // Only a live run blocks the button. A stale "queued" row must stay
  // re-queueable: re-queue is the backend's recovery path for queued jobs
  // that never got picked up.
  const auditRunning = (checksStatus ?? "").toLowerCase() === "running";

  const findingsSummary = checksLoading
    ? "Loading…"
    : checksLoadError
      ? "Unavailable"
      : checkState === "running"
        ? "Audit running…"
        : mergedFindings.length > 0
          ? `${mergedFindings.length} finding${mergedFindings.length === 1 ? "" : "s"}${
              checkState === "unaudited" ? " · audit not run" : ""
            }`
          : checkState === "failed"
            ? "Audit failed"
            : checkState === "unaudited"
              ? "Audit not run"
              : "Clean";

  const findingsBody = () => {
    if (checksLoading) {
      return (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      );
    }
    if (checksLoadError) {
      return (
        <p className="font-mono text-[11px] break-all text-red-500">
          {checksLoadError}
        </p>
      );
    }
    return (
      <>
        {checkState === "failed" ? (
          <p className="font-mono text-[11px] break-all text-red-500">
            {checksError || "The source audit failed."}
          </p>
        ) : checkState === "running" && mergedFindings.length === 0 ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-3 w-2/5" />
          </div>
        ) : null}
        {mergedFindings.length > 0 ? (
          // The default tier copy narrates trial classification; these
          // findings speak to the task itself.
          <SeverityGroups
            items={mergedFindings}
            tierEffects={{
              must_fix:
                "The defect can decide trials — QA marks the task bad until it is fixed.",
              should_fix: "Does not change the verdict.",
              optional: "Does not change the verdict.",
            }}
            renderItemFooter={renderFindingSources}
          />
        ) : checkState === "clean" ? (
          <p className="text-muted-foreground text-sm leading-relaxed">
            The source audit and trial QA found no defects in this task.
          </p>
        ) : checkState === "unaudited" ? (
          <p className="text-muted-foreground text-sm leading-relaxed">
            The source audit has not run on this version yet.
          </p>
        ) : null}
      </>
    );
  };

  const trialQaBody = () => {
    if (trialsLoading && !trials) {
      return (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      );
    }
    if (trialsError && !trials) {
      return (
        <p className="font-mono text-[11px] break-all text-red-500">
          Unable to load the task&apos;s trials.
        </p>
      );
    }
    if (versionTrials.length === 0) {
      return (
        <p className="text-muted-foreground text-sm leading-relaxed">
          {version != null
            ? `No trials for v${version} yet.`
            : "No trials for this task yet."}
        </p>
      );
    }
    if (qaTrials.length === 0) {
      return (
        <p className="text-muted-foreground text-sm leading-relaxed">
          Trial QA has not run yet. Run QA to classify this task&apos;s
          trials and synthesize a verdict.
        </p>
      );
    }

    return (
      <>
        <div className="flex flex-wrap items-center gap-1.5">
          {CLASSIFICATION_ORDER.map((classification) => {
            const count = classificationCounts.get(classification);
            if (!count) return null;
            const token = VERDICT_TOKENS[classification] ?? FALLBACK_TOKEN;
            const Icon = token.icon;
            return (
              <span
                key={classification}
                className={cn(
                  "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px]",
                  token.chip,
                  token.accent,
                )}
              >
                <Icon className="h-3 w-3" aria-hidden="true" />
                {count} {CLASSIFICATION_LABELS[classification].toLowerCase()}
              </span>
            );
          })}
          {unanalyzedCount > 0 ? (
            <span className="text-muted-foreground font-mono text-[10px]">
              {unanalyzedCount} not analyzed
            </span>
          ) : null}
        </div>

        <div className="flex flex-col gap-1.5">
          {qaTrials.map((trial) => (
            <TrialQaRow
              key={trial.id}
              trial={trial}
              onOpen={() => openTrial(trial)}
            />
          ))}
        </div>
      </>
    );
  };

  return (
    <div className={cn("flex flex-col", className)}>
      {verdictTask ? (
        <div className="border-border border-b p-4">
          <TaskVerdictBadge task={verdictTask} variant="inline" />
        </div>
      ) : null}

      <div className="border-border flex flex-col gap-3 border-b p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-muted-foreground font-mono text-[11px] font-semibold tracking-wider uppercase">
            Findings
          </h2>
          <span className="text-muted-foreground font-mono text-[11px]">
            {findingsSummary}
            {!checksStateUnknown && hasDisplayableCostUsd(checksCostUsd)
              ? ` · ${formatCostUsd(checksCostUsd)}`
              : ""}
          </span>
          <div className="ml-auto flex items-center gap-2">
            {mergedFindings.length > 0 ? (
              <CopyJsonButton
                // Trials collapse to their ids — the full rows are noise here.
                value={mergedFindings.map(
                  ({ trials: sources, fromAudit, ...item }) => ({
                    ...item,
                    from_audit: fromAudit,
                    trial_ids: sources.map((t) => t.id),
                  }),
                )}
                label="the task's findings"
              />
            ) : null}
            <button
              type="button"
              disabled={checksRerunning || auditRunning || checksStateUnknown}
              onClick={onRerunChecks}
              className="text-muted-foreground hover:text-foreground border-border rounded border px-2 py-0.5 font-mono text-[10px] font-medium disabled:cursor-not-allowed disabled:opacity-50"
              title="Runs the source audit on the task's current version"
            >
              {checksRerunning
                ? "Queuing…"
                : checkState === "unaudited"
                  ? "Run audit"
                  : "Re-run audit"}
            </button>
          </div>
        </div>

        {checksQueueError ? (
          <p className="text-[11px] text-red-500">{checksQueueError}</p>
        ) : null}

        {findingsBody()}
      </div>

      <div className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-muted-foreground font-mono text-[11px] font-semibold tracking-wider uppercase">
            Trial QA
          </h2>
          <span className="text-muted-foreground font-mono text-[11px]">
            {trialsLoading && !trials
              ? "Loading…"
              : `${analyzedCount}/${versionTrials.length} trial${
                  versionTrials.length === 1 ? "" : "s"
                } analyzed${version != null ? ` · v${version}` : ""}`}
          </span>
        </div>
        {trialQaBody()}
      </div>
    </div>
  );
}

function TrialQaRow({ trial, onOpen }: { trial: Trial; onOpen: () => void }) {
  const analysis = trial.analysis;
  const running = isActivePipelineStatus(trial.analysis_status);
  const failed = !analysis && trial.analysis_status === "failed";
  const token = analysis
    ? (VERDICT_TOKENS[analysis.classification] ?? FALLBACK_TOKEN)
    : FALLBACK_TOKEN;
  const Icon = token.icon;
  const hasBody = Boolean(
    analysis?.evidence || analysis?.root_cause || analysis?.recommendation,
  );

  const header = (
    <>
      {running ? (
        <Loader2
          className="h-3.5 w-3.5 shrink-0 animate-spin text-blue-500"
          aria-hidden="true"
        />
      ) : (
        <Icon
          className={cn(
            "h-3.5 w-3.5 shrink-0",
            failed ? "text-red-500" : token.accent,
          )}
          aria-hidden="true"
        />
      )}
      <span
        className={cn(
          "shrink-0 font-mono text-[10px] font-semibold tracking-wider",
          running ? "text-blue-500" : failed ? "text-red-500" : token.accent,
        )}
      >
        {running
          ? "ANALYZING"
          : failed
            ? "QA FAILED"
            : analysis
              ? CLASSIFICATION_LABELS[analysis.classification].toUpperCase()
              : "PENDING"}
      </span>
      {analysis?.subtype ? (
        <span className="text-muted-foreground shrink-0 font-mono text-[10px]">
          {analysis.subtype}
        </span>
      ) : null}
      <span className="text-muted-foreground min-w-0 flex-1 truncate text-[11px]">
        {trialLabel(trial)}
      </span>
      <button
        type="button"
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onOpen();
        }}
        className="border-border text-muted-foreground hover:text-foreground hover:border-foreground/40 inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors"
        title={`Open trial ${trial.name}`}
      >
        View trial
        <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
      </button>
    </>
  );

  if (!hasBody) {
    return (
      <div className="border-border bg-background/40 flex items-center gap-2.5 rounded-lg border px-3 py-2">
        {header}
        {failed && trial.analysis_error ? (
          <span className="text-muted-foreground truncate font-mono text-[10px]">
            {trial.analysis_error}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <details className="group border-border bg-background/40 rounded-lg border">
      <summary className="hover:bg-foreground/5 flex cursor-pointer list-none items-center gap-2.5 px-3 py-2 transition-colors select-none">
        <span
          aria-hidden="true"
          className="text-muted-foreground text-[9px] transition-transform group-open:rotate-90"
        >
          &#9654;
        </span>
        {header}
      </summary>
      <div className="border-border flex flex-col gap-2 border-t px-3 py-3">
        {analysis?.root_cause ? (
          <div className="flex items-baseline gap-2">
            <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
              CAUSE
            </span>
            <AnalysisProse
              text={analysis.root_cause}
              className="text-foreground/90 min-w-0"
            />
          </div>
        ) : null}
        {analysis?.evidence ? (
          <div className="flex items-baseline gap-2">
            <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
              EVIDENCE
            </span>
            <AnalysisProse
              text={analysis.evidence}
              className="text-foreground/90 min-w-0"
            />
          </div>
        ) : null}
        {analysis?.recommendation ? (
          <div className="flex items-baseline gap-2">
            <span className="text-muted-foreground shrink-0 font-mono text-[10px] tracking-widest">
              FIX
            </span>
            <AnalysisProse
              text={analysis.recommendation}
              className="text-foreground/90 min-w-0"
            />
          </div>
        ) : null}
      </div>
    </details>
  );
}
