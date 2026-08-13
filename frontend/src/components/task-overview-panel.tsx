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
  // Server ids are content hashes that include the analyzer source, so they
  // dedupe within one source only — the cross-source join is `links_to`.
  // Items without an id fall back to a content key.
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
  scopeTrials,
  scopeLoading,
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
  /** Version the pane is scoped to: a number pins, null deliberately
   *  aggregates every trial, and undefined means still resolving — the
   *  trial aggregation waits instead of briefly spanning all versions. */
  version?: number | null;
  /** The trials that belong to the host's context (an experiment drawer
   *  passes its own; the task page passes the version's). Trials outside
   *  this set still render, marked as from elsewhere. Null = no context. */
  scopeTrials?: Trial[] | null;
  /** The host is still streaming its trial rows — an empty scope renders
   *  as loading, not as "no trials". */
  scopeLoading?: boolean;
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
  const versionKnown = version !== undefined;
  // Probes are excluded at the query: they are internal instruction-overlay
  // runs, not attempts, and their `analysis` is a different shape entirely.
  // The fetch waits for the version so it can scope server-side — a task
  // carries trials across many versions and experiments, and every full row
  // ships its whole analysis payload.
  const trialsKey =
    taskId && versionKnown
      ? `${apiBaseUrl}/tasks/${taskId}/trials?probe=false${
          version !== null ? `&version=${version}` : ""
        }`
      : null;
  const { data: trials, error: trialsError } = useSWR<Trial[]>(trialsKey, fetcher, {
    revalidateOnFocus: false,
    refreshInterval: (data) => {
      const anyAnalysisLive = (data ?? []).some((trial) =>
        isActivePipelineStatus(trial.analysis_status),
      );
      return anyAnalysisLive || qaActive ? 15000 : 0;
    },
  });

  // Host rows can include probes and superseded trials; filter them here too.
  const scoped = useMemo(() => {
    if (scopeTrials == null) return null;
    return scopeTrials.filter(
      (trial) => !trial.is_probe && !trial.superseded_by_trial_id,
    );
  }, [scopeTrials]);
  const fetchedById = useMemo(
    () => new Map((trials ?? []).map((trial) => [trial.id, trial])),
    [trials],
  );
  // Show every trial of the version. The verdict is computed over all of
  // them, so a shorter list can hide the evidence behind it.
  const displayTrials = useMemo(() => {
    if (scoped == null) return trials ?? null;
    const inScope = new Set(scoped.map((trial) => trial.id));
    // Until the host's rows have loaded, rows from elsewhere would render
    // without their mark -- hold them back.
    const elsewhere = scopeLoading
      ? []
      : (trials ?? []).filter(
          (trial) => !inScope.has(trial.id) && !trial.superseded_by_trial_id,
        );
    return [
      ...scoped.map((trial) => fetchedById.get(trial.id) ?? trial),
      ...elsewhere,
    ];
  }, [scoped, scopeLoading, fetchedById, trials]);
  // Null until the host's rows have loaded, so nothing is marked too early.
  const foreignIds = useMemo(() => {
    if (scoped == null || scopeLoading) return null;
    const inScope = new Set(scoped.map((trial) => trial.id));
    return new Set(
      (trials ?? [])
        .filter(
          (trial) => !inScope.has(trial.id) && !trial.superseded_by_trial_id,
        )
        .map((trial) => trial.id),
    );
  }, [scoped, scopeLoading, trials]);
  const versionTrials = useMemo(() => {
    if (version === undefined) return [];
    const all = displayTrials ?? [];
    if (version === null) return all;
    return all.filter((trial) => trial.task_version === version);
  }, [displayTrials, version]);

  const {
    classificationCounts,
    unanalyzedCount,
    analyzedCount,
    mergedFindings,
    qaTrials,
  } = useMemo(() => {
    const byKey = new Map<string, SourcedFinding>();
    const addTrial = (row: SourcedFinding, trial: Trial) => {
      if (!row.trials.some((t) => t.id === trial.id)) row.trials.push(trial);
    };
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
      // Exploitation assessments are the trial→audit-finding join: an
      // exploiting trial belongs on the audit row's "seen in" list. A
      // not-exploited assessment only says the classifier looked — skip it.
      for (const assessment of analysis.exploitation ?? []) {
        if (!assessment.exploited || !assessment.links_to) continue;
        const audited = byKey.get(assessment.links_to);
        if (!audited) continue;
        audited.exploited = true;
        addTrial(audited, trial);
      }
      for (const item of analysis.action_items ?? []) {
        // A post-trial item that links to an audit finding is the same
        // defect seen from the trial side — fold it into that row. Ids
        // can't make this join: the server hash includes the source.
        const linked = item.links_to ? byKey.get(item.links_to) : undefined;
        const existing = linked ?? byKey.get(findingKey(item));
        if (existing) {
          addTrial(existing, trial);
          // One exploiting trial marks the finding exploited.
          if (item.exploited) existing.exploited = true;
        } else {
          const key = findingKey(item);
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
        Number(foreignIds?.has(a.id) ?? false) -
          Number(foreignIds?.has(b.id) ?? false) ||
        a.created_at.localeCompare(b.created_at),
    );
    return {
      classificationCounts: counts,
      unanalyzedCount: unanalyzed,
      analyzedCount: withQa.filter((trial) => trial.analysis).length,
      mergedFindings: Array.from(byKey.values()),
      qaTrials: withQa,
    };
  }, [versionTrials, checksFindings, foreignIds]);

  // The rows handed to SeverityGroups carry only copy-safe fields — its
  // per-item copy button serializes the row as-is, so the trial objects
  // stay behind in the lookup map and the copy gets ids.
  const findingItems = useMemo(
    () =>
      mergedFindings.map(({ fromAudit, trials: sources, ...item }) => ({
        ...item,
        from_audit: fromAudit,
        trial_ids: sources.map((t) => t.id),
      })),
    [mergedFindings],
  );
  const findingSourcesById = useMemo(
    () => new Map(mergedFindings.map((f) => [f.id ?? "", f])),
    [mergedFindings],
  );
  const foreignShownCount = useMemo(
    () =>
      foreignIds
        ? versionTrials.filter((trial) => foreignIds.has(trial.id)).length
        : 0,
    [foreignIds, versionTrials],
  );

  const taskTrialHref = (trial: Trial): string | null => {
    if (!taskId) return null;
    const params = new URLSearchParams();
    if (trial.task_version_id) params.set("version", trial.task_version_id);
    params.set("trial", trial.id);
    return `/tasks/${taskId}?${params.toString()}`;
  };

  const openTrial = (trial: Trial) => {
    // Trials from elsewhere open in a new tab; the drawer keeps its context.
    if (foreignIds?.has(trial.id)) {
      const href = taskTrialHref(trial);
      if (href) window.open(href, "_blank", "noopener,noreferrer");
      return;
    }
    if (onOpenTrial?.(trial)) return;
    const href = taskTrialHref(trial);
    if (href) router.push(href);
  };

  const renderFindingSources = (item: PreTrialFinding) => {
    const sourced = findingSourcesById.get(item.id ?? "");
    if (!sourced) return null;
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
        {(sourced.trials ?? []).map((trial) => {
          const foreign = foreignIds?.has(trial.id) ?? false;
          return (
            <button
              key={trial.id}
              type="button"
              onClick={() => openTrial(trial)}
              className={cn(
                "border-border text-muted-foreground hover:text-foreground hover:border-foreground/40 inline-flex min-w-0 max-w-full items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors",
                foreign && "border-dashed",
              )}
              title={
                foreign
                  ? `Open trial ${trial.name} in a new tab — ran outside this experiment`
                  : `Open trial ${trial.name}`
              }
            >
              <span className="min-w-0 truncate">{trialLabel(trial)}</span>
              <ArrowUpRight className="h-3 w-3 shrink-0" aria-hidden="true" />
            </button>
          );
        })}
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
    // The default tier copy narrates trial classification; these findings
    // speak to the task itself.
    const findingsList =
      findingItems.length > 0 ? (
        <SeverityGroups
          items={findingItems}
          tierEffects={{
            must_fix:
              "The defect can decide trials — QA marks the task bad until it is fixed.",
            should_fix: "Does not change the verdict.",
            optional: "Does not change the verdict.",
          }}
          renderItemFooter={renderFindingSources}
        />
      ) : null;
    if (checksLoadError) {
      // The audit state is unknown, but trial-side findings come from the
      // trials endpoint — show what survives under the error.
      return (
        <>
          <p className="font-mono text-[11px] break-all text-red-500">
            {checksLoadError}
          </p>
          {findingsList}
        </>
      );
    }
    return (
      <>
        {checkState === "failed" ? (
          <p className="font-mono text-[11px] break-all text-red-500">
            {checksError || "The source audit failed."}
          </p>
        ) : checkState === "running" && findingItems.length === 0 ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-3 w-2/5" />
          </div>
        ) : null}
        {findingsList ? (
          findingsList
        ) : checkState === "clean" ? (
          <p className="text-muted-foreground text-sm leading-relaxed">
            {analyzedCount > 0
              ? "The source audit and trial QA found no defects in this task."
              : "The source audit found no defects in this task's source."}
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
    if (!versionKnown) {
      // The scoping version hasn't resolved; aggregating now would span
      // every version. On a dead /detail it never will — say so.
      return checksLoadError ? (
        <p className="font-mono text-[11px] break-all text-red-500">
          Unable to resolve the task version to scope the trials.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      );
    }
    // Without a host scope, the fetch defines the set — wait for it.
    // Scoped rows render immediately (or fail into the honest states below).
    if (displayTrials == null) {
      return trialsError ? (
        <p className="font-mono text-[11px] break-all text-red-500">
          Unable to load the task&apos;s trials.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-8 w-full rounded-lg" />
          <Skeleton className="h-3 w-2/5" />
        </div>
      );
    }
    if (versionTrials.length === 0) {
      // An empty scope while the host is still streaming its trial rows
      // is not an answer yet.
      if (scopeLoading) {
        return (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-8 w-full rounded-lg" />
            <Skeleton className="h-3 w-2/5" />
          </div>
        );
      }
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
              foreign={foreignIds?.has(trial.id) ?? false}
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
            {findingItems.length > 0 ? (
              <CopyJsonButton
                value={findingItems}
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
            {!versionKnown
              ? checksLoadError
                ? "Unavailable"
                : "Loading…"
              : displayTrials == null
                ? trialsError
                  ? "Unavailable"
                  : "Loading…"
                : versionTrials.length === 0 && scopeLoading
                  ? "Loading…"
                  : `${analyzedCount}/${versionTrials.length} trial${
                      versionTrials.length === 1 ? "" : "s"
                    } analyzed${version != null ? ` · v${version}` : ""}${
                      foreignShownCount > 0
                        ? ` · ${foreignShownCount} from outside this experiment`
                        : ""
                    }`}
          </span>
        </div>
        {trialQaBody()}
      </div>
    </div>
  );
}

function TrialQaRow({
  trial,
  foreign,
  onOpen,
}: {
  trial: Trial;
  /** From outside the host's context. */
  foreign?: boolean;
  onOpen: () => void;
}) {
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
        <span
          className="text-muted-foreground min-w-0 truncate font-mono text-[10px]"
          title={analysis.subtype}
        >
          {analysis.subtype}
        </span>
      ) : null}
      <span className="text-muted-foreground min-w-0 flex-1 truncate text-[11px]">
        {trialLabel(trial)}
      </span>
      {foreign ? (
        <span
          className="border-border text-muted-foreground shrink-0 rounded border border-dashed px-1.5 py-0.5 font-mono text-[9.5px]"
          title="This trial ran outside this experiment"
        >
          elsewhere
        </span>
      ) : null}
      <button
        type="button"
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          onOpen();
        }}
        className="border-border text-muted-foreground hover:text-foreground hover:border-foreground/40 inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors"
        title={
          foreign
            ? `Open trial ${trial.name} in a new tab`
            : `Open trial ${trial.name}`
        }
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
