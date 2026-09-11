"use client";

import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import dynamic from "next/dynamic";
import { useSearchParams } from "next/navigation";
import type { TaskPane } from "@/components/task-files-panel";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ExperimentTrialsTable } from "@/components/experiment-trials-table";
import { ExperimentPageSkeleton } from "@/components/experiment-page-skeleton";
import { SummaryStat } from "@/components/summary-stat";
import { CostValue } from "@/components/cost-value";
import { QaCostSuffix } from "@/components/qa-cost-suffix";
import { TagEditor } from "@/components/tag-editor";
import { UnifiedDrawerWrapper } from "@/components/unified-drawer-wrapper";
import { useUserUiLayout } from "@/lib/use-user-ui-layout";
import { fetcher } from "@/lib/api";
import {
  prBadge,
  prNumberFromUrl,
  taskPrUrl,
  urlWithSearch,
} from "@/lib/utils";
import {
  formatCostUsd,
  formatTokenCount,
  hasDisplayableCostUsd,
} from "@/lib/format";
import {
  EMPTY_TRIAL_AGGREGATE,
  accumulateTrial,
} from "@/lib/trial-aggregation";
import type {
  ExperimentFocusResponse,
  PublicExperimentFocusResponse,
  ExperimentPageSummary,
  Task,
  Trial,
  UserTagRef,
} from "@/lib/types";
import { trialFromExperimentCell } from "@/lib/experiment-page-data";
import type { ExperimentCostTotalsResource } from "@/lib/use-experiment-cost-totals";
import { ExternalLink, GitPullRequest, Loader2 } from "lucide-react";
import {
  buildExperimentAgentSummaries,
  getExperimentAgentKey,
  isBaselineAgentName,
  type ExperimentAgentSummary,
} from "@/lib/experiment-agent-grouping";
import { taskReviewFilter, type TaskReviewFilter } from "@/lib/review";
import { resolveExperimentTaskVersion } from "@/lib/experiment-task-version";
import {
  formatLineRange,
  parseLineRange,
  type LineRange,
} from "@/lib/line-range";
import { sameFilePath } from "@/lib/file-path";
import { expandTrialParam } from "@/lib/trial-url";

type DrawerMode = "task" | "trial";

import { ProbeDetailPanel } from "@/components/probe-detail-panel";

const TrialDetailPanel = dynamic(
  () =>
    import("@/components/trial-detail-panel").then(
      (mod) => mod.TrialDetailPanel
    ),
  {
    ssr: false,
    loading: () => <DrawerContentLoading label="Loading trial details..." />,
  }
);

const TaskFilesPanel = dynamic(
  () =>
    import("@/components/task-files-panel").then((mod) => mod.TaskFilesPanel),
  {
    ssr: false,
    loading: () => <DrawerContentLoading label="Loading task files..." />,
  }
);

function DrawerContentLoading({ label }: { label: string }) {
  return (
    <div className="text-muted-foreground flex h-full min-h-[180px] items-center justify-center gap-2 text-sm">
      <Loader2 className="h-4 w-4 animate-spin" />
      <span>{label}</span>
    </div>
  );
}

/** Which tasks next/prev may grow into as /open pages stream in. */
type TaskNavScope = "experiment" | Exclude<TaskReviewFilter, "all">;

type DrawerState = {
  isOpen: boolean;
  mode: DrawerMode;
  task: Task;
  taskIndex: number;
  orderedTasks: Task[];
  /** `rejected` keeps review next/prev on rejected rows only. */
  taskNavScope: TaskNavScope;
  trial: Trial | null;
  trialIndex: number | null;
  orderedTrials: Trial[];
  trialGroups: Array<{
    agent: string;
    model: string | null;
    trials: Trial[];
  }>;
} | null;

interface ExperimentDetailViewProps {
  experimentId?: string;
  tasksForExperiment: Task[];
  pageSummary?: ExperimentPageSummary;
  // Exact server-side spend rollup and its request lifecycle. Paginated trial
  // rows are never used as a cost total.
  costTotals: ExperimentCostTotalsResource;
  onRetryCostTotals: () => void;
  isLoading: boolean;
  isLoadingTrials?: boolean;
  pagesComplete?: boolean;
  hasError?: boolean;
  errorTitle?: string;
  errorDescription?: string;
  headerLeft: React.ReactNode;
  headerStatus?: React.ReactNode;
  headerRight?: React.ReactNode;
  headerDescription?: React.ReactNode;
  inlineAlert?: React.ReactNode;
  readOnly?: boolean;
  allowRetry?: boolean;
  showAnalysis?: boolean;
  apiBaseUrl?: string;
  focusUrl?: string;
  onTaskUnlink?: (task: Task) => Promise<void>;
  onTrialDelete?: (trial: Trial, task: Task | null) => Promise<void>;
  onRerun?: (taskIds?: string[]) => void;
  // Logged-in exp page sends slim trials, so set true to fetch a clicked
  // trial's full detail on open. Public share page omits it (it passes full
  // trials and can't use the authed /api/trials route).
  loadFullTrialOnOpen?: boolean;
}

const AGENT_SUMMARY_STORAGE_PREFIX = "oddish:experiment-agent-summaries:";

function isRetryableFocusError(error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status;
  return status == null || status === 408 || status === 429 || status >= 500;
}

function getModelScopedAgentsFromSummaries(
  summaries: ExperimentAgentSummary[]
): Set<string> {
  return new Set(
    summaries
      .filter((summary) => summary.isModelScoped)
      .map((summary) => summary.agent)
  );
}

type ExperimentSummary = {
  rewardSuccess: number;
  rewardSum: number;
  rewardTotal: number;
  /**
   * Mean over tasks of the per-task mean reward (scored trials only,
   * nop/oracle baselines excluded). Null until at least one task has a
   * scored trial.
   */
  avgScore: number | null;
  totalTrials: number;
  completedTrials: number;
  failedTrials: number;
  skippedTrials: number;
  passCount: number;
  partialCount: number;
  failCount: number;
  harnessErrorCount: number;
  pendingCount: number;
  costUsd: number;
  costTrialCount: number;
  costHasEstimated: boolean;
  costHasNative: boolean;
  qaCostUsd: number;
  ownedQaCostUsd: number;
  qaHasEstimated: boolean;
  ownedCostUsd: number;
  ownedTrialCount: number;
  ownedHasEstimated: boolean;
  ownedHasNative: boolean;
  tokenCount: number;
  tokenTrialCount: number;
  ownedTokenCount: number;
  ownedTokenTrialCount: number;
  billedCostUsd: number;
  billedTrialCount: number;
  billedHasEstimated: boolean;
  billedHasNative: boolean;
  billedTokenCount: number;
  billedTokenTrialCount: number;
};

function buildExperimentSummary(tasksForExperiment: Task[]): ExperimentSummary {
  const acc = { ...EMPTY_TRIAL_AGGREGATE };
  // ``rewardSuccess`` mirrors ``passCount`` from the trials path but folds in
  // task-level fallbacks for tasks whose trials aren't loaded yet.
  let rewardSuccess = 0;
  let totalTrialsFallback = 0;
  let completedFallback = 0;
  let failedFallback = 0;
  let skippedFallback = 0;
  let rewardSumFallback = 0;
  let rewardTotalFallback = 0;
  // Per-task mean reward over scored trials (baselines excluded); the avg
  // score is the mean of these so every task carries equal weight
  // regardless of how many trials it ran.
  let taskScoreSum = 0;
  let taskScoreCount = 0;

  for (const task of tasksForExperiment) {
    const trials = (task.trials ?? []).filter((t) => !t.is_probe);
    if (trials.length > 0) {
      // task.experiment_id is the viewing experiment (set by the backend
      // builders); trials homed elsewhere count as cost (they price the work
      // shown) but not as owned/new spend.
      for (const trial of trials)
        accumulateTrial(
          acc,
          trial,
          trial.experiment_id == null ||
            trial.experiment_id === task.experiment_id
        );

      let scoredRewardSum = 0;
      let scoredCount = 0;
      for (const trial of trials) {
        if (isBaselineAgentName(trial.agent)) continue;
        if (trial.status !== "success" || trial.reward == null) continue;
        scoredRewardSum += trial.reward;
        scoredCount += 1;
      }
      if (scoredCount > 0) {
        taskScoreSum += scoredRewardSum / scoredCount;
        taskScoreCount += 1;
      }
    } else {
      rewardSuccess += task.reward_success ?? 0;
      rewardSumFallback += task.reward_sum ?? task.reward_success ?? 0;
      rewardTotalFallback += task.reward_total ?? 0;
      totalTrialsFallback += task.total;
      completedFallback += task.completed;
      failedFallback += task.failed;
      skippedFallback += task.skipped ?? 0;
    }
  }

  return {
    rewardSuccess: rewardSuccess + acc.passCount,
    rewardSum: acc.rewardSum + rewardSumFallback,
    rewardTotal: acc.rewardTotal + rewardTotalFallback,
    avgScore: taskScoreCount > 0 ? taskScoreSum / taskScoreCount : null,
    totalTrials: acc.trialCount + totalTrialsFallback,
    completedTrials: acc.completed + completedFallback,
    failedTrials: acc.failed + failedFallback,
    skippedTrials: acc.skipped + skippedFallback,
    passCount: acc.passCount,
    partialCount: acc.partialCount,
    failCount: acc.failCount,
    harnessErrorCount: acc.harnessErrorCount,
    pendingCount: acc.pendingCount,
    costUsd: 0,
    costTrialCount: 0,
    costHasEstimated: false,
    costHasNative: false,
    // QA has no client-side fold -- it rides in only via the server rollup
    // (the ``costTotals`` override below), so the base value is always zero.
    qaCostUsd: 0,
    ownedQaCostUsd: 0,
    qaHasEstimated: false,
    ownedCostUsd: 0,
    ownedTrialCount: 0,
    ownedHasEstimated: false,
    ownedHasNative: false,
    tokenCount: 0,
    tokenTrialCount: 0,
    ownedTokenCount: 0,
    ownedTokenTrialCount: 0,
    billedCostUsd: 0,
    billedTrialCount: 0,
    billedHasEstimated: false,
    billedHasNative: false,
    billedTokenCount: 0,
    billedTokenTrialCount: 0,
  };
}

function ExperimentHeaderMeta({
  isLoading,
  isInitialLoading,
  headerStatus,
  showPassAtK,
  onToggleShowPassAtK,
  headerRight,
  prLink,
}: {
  isLoading: boolean;
  isInitialLoading: boolean;
  headerStatus?: React.ReactNode;
  showPassAtK: boolean;
  onToggleShowPassAtK: () => void;
  headerRight?: React.ReactNode;
  prLink?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {headerStatus}
      {prLink}
      {isLoading && (
        <div className="inline-flex items-center gap-1.5 rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface-2)] px-2 py-1 text-xs text-[color:var(--paper-ink-3)]">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          <span>{isInitialLoading ? "Loading tasks..." : "Refreshing..."}</span>
        </div>
      )}
      <Button
        type="button"
        variant="ghost"
        onClick={onToggleShowPassAtK}
        aria-pressed={showPassAtK}
        className={`h-8 gap-[7px] rounded-[7px] border px-3 text-[12px] leading-none transition-colors select-none ${
          showPassAtK
            ? "border-[color:var(--paper-ink)] bg-[color:var(--paper-ink)] text-[color:var(--paper-bg)] hover:bg-[color:color-mix(in_oklch,var(--paper-ink),white_12%)]"
            : "border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] text-[color:var(--paper-ink)] hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)]"
        }`}
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M3 3v18h18" />
          <path d="M7 14l4-4 4 4 5-5" />
        </svg>
        Pass/k graph
      </Button>
      {headerRight}
    </div>
  );
}

function MetaDot() {
  return (
    <span
      aria-hidden="true"
      className="h-[3px] w-[3px] rounded-full bg-[color:var(--paper-ink-4)]"
    />
  );
}

function formatRelativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "";
  const diffSec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (diffSec < 45) return "just now";
  if (diffSec < 60 * 60) return `${Math.round(diffSec / 60)}m ago`;
  if (diffSec < 60 * 60 * 24) return `${Math.round(diffSec / 3600)}h ago`;
  if (diffSec < 60 * 60 * 24 * 30)
    return `${Math.round(diffSec / (3600 * 24))}d ago`;
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function pickExperimentCreationMeta(
  tasks: Task[],
  includeIdentity: boolean
): {
  createdAt: string | null;
  author: string | null;
} {
  if (tasks.length === 0) return { createdAt: null, author: null };
  const experimentCreatedAt =
    tasks.find((task) => task.experiment_created_at)?.experiment_created_at ??
    null;
  let earliest: Task = tasks[0];
  for (const task of tasks) {
    if (
      new Date(task.created_at).getTime() <
      new Date(earliest.created_at).getTime()
    ) {
      earliest = task;
    }
  }
  // Prefer the experiment's own owner (the creating run's submitter, stamped
  // on the experiment). Fall back to the earliest task's author for
  // experiments with no stamped owner.
  const experimentOwner = includeIdentity
    ? (tasks.find((task) => task.experiment_owner)?.experiment_owner ?? null)
    : null;
  return {
    createdAt: experimentCreatedAt ?? earliest.created_at,
    author: includeIdentity
      ? (experimentOwner ?? earliest.github_username ?? earliest.user ?? null)
      : null,
  };
}

function pickExperimentPr(tasks: Task[]): {
  prUrl: string | null;
  prTitle: string | null;
  prNumber: string | null;
} {
  // Prefer the experiment's own PR link (stamped set-once from the creating
  // run); it is immune to other experiments re-running a shared task. Fall back
  // to the task-derived link for experiments with no stamped link.
  const experimentLink =
    tasks.find((t) => t.experiment_link)?.experiment_link ?? null;
  if (experimentLink) {
    return {
      prUrl: experimentLink,
      prTitle: null,
      prNumber: prNumberFromUrl(experimentLink),
    };
  }
  const task = tasks.find((t) => taskPrUrl(t.link, t.github_meta));
  const meta = task?.github_meta;
  const prUrl = taskPrUrl(task?.link, meta);
  return {
    prUrl,
    prTitle: meta?.pr_title ?? null,
    prNumber: meta?.pr_number ?? prNumberFromUrl(prUrl),
  };
}

// Dedicated header affordance linking an experiment back to the GitHub PR that
// spawned it (lineage tracing). The PR URL rides in along every task's
// `github_meta` (set via `oddish run --github-meta`); we surface the first task
// that carries one. Renders nothing when no PR metadata is present.
function ExperimentPrLink({
  tasks,
  isInitialLoading,
}: {
  tasks: Task[];
  isInitialLoading: boolean;
}) {
  if (isInitialLoading) return null;
  const { prUrl, prTitle, prNumber } = pickExperimentPr(tasks);
  if (!prUrl) {
    return (
      <span
        title="No pull request linked to this experiment"
        className="inline-flex h-8 items-center gap-[7px] rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] leading-none text-[color:var(--paper-ink-3)] opacity-60 select-none"
      >
        <GitPullRequest className="h-3.5 w-3.5 shrink-0" aria-hidden />
        no PR linked
      </span>
    );
  }

  const { label, number } = prBadge(prUrl, prNumber);

  return (
    <a
      href={prUrl}
      target="_blank"
      rel="noreferrer"
      title={
        prTitle ? `${prTitle} — view on GitHub` : "View pull request on GitHub"
      }
      className="inline-flex h-8 max-w-[200px] items-center gap-[7px] rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] leading-none text-[color:var(--paper-ink)] transition-colors select-none hover:border-[color:var(--paper-ink-4)] hover:bg-[color:var(--paper-surface-2)]"
    >
      <GitPullRequest className="h-3.5 w-3.5 shrink-0" aria-hidden />
      <span className="min-w-0 truncate">
        {label}
        {number && (
          <span className="text-[color:var(--paper-ink-3)]"> #{number}</span>
        )}
      </span>
      <ExternalLink className="h-3 w-3 shrink-0 opacity-50" aria-hidden />
    </a>
  );
}

function ExperimentMetaStrip({
  tasks,
  isInitialLoading,
  experimentId,
  readOnly = false,
}: {
  tasks: Task[];
  isInitialLoading: boolean;
  experimentId?: string;
  readOnly?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopyExperimentId = useCallback(async () => {
    if (!experimentId) return;
    try {
      await navigator.clipboard.writeText(experimentId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (error) {
      console.error("Failed to copy experiment id", error);
    }
  }, [experimentId]);

  if (isInitialLoading) return null;
  const { createdAt, author } = pickExperimentCreationMeta(tasks, !readOnly);
  const showAuthor = Boolean(author) && !readOnly;
  if (!createdAt && !showAuthor && !experimentId) return null;

  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 font-mono text-[11.5px] text-[color:var(--paper-ink-3)]">
      {createdAt && (
        <span title={new Date(createdAt).toLocaleString()}>
          created {formatRelativeTime(createdAt)}
        </span>
      )}
      {createdAt && showAuthor && <MetaDot />}
      {showAuthor && <span>by {author}</span>}
      {(createdAt || showAuthor) && experimentId && <MetaDot />}
      {experimentId && (
        <span className="inline-flex items-center gap-1">
          <span>id</span>
          <Button
            type="button"
            variant="ghost"
            onClick={handleCopyExperimentId}
            className="h-auto cursor-pointer rounded-sm bg-transparent p-0 font-mono text-[11.5px] font-normal text-[color:var(--paper-ink-2)] transition hover:bg-transparent hover:text-[color:var(--paper-ink)]"
            aria-label={`Copy experiment id ${experimentId}`}
            title={copied ? "Copied" : "Click to copy experiment id"}
          >
            <span className="select-all">{experimentId}</span>
          </Button>
          {copied && <span aria-live="polite">copied</span>}
        </span>
      )}
    </div>
  );
}

function ExperimentSummaryBar({
  taskCount,
  summary,
  isInitialLoading,
  isLoadingTrials,
  showNewSpend,
  // True when cost came from the server rollup, which reports SPEND: every
  // trial that ran, including earlier task versions, superseded retries and
  // probes that the table below filters out. Drives the tooltip's disclosure.
  costStatus,
  qa,
  reviewFilter,
  onReviewFilter,
}: {
  reviewFilter: string;
  onReviewFilter: (value: string) => void;
  taskCount: number;
  summary: ExperimentSummary;
  isInitialLoading: boolean;
  isLoadingTrials: boolean;
  showNewSpend: boolean;
  costStatus: ExperimentCostTotalsResource["status"];
  qa: {
    accepted: number;
    rejected: number;
    running: number;
    failed: number;
    unreviewed: number;
  } | null;
}) {
  if (isInitialLoading) {
    return (
      <div className="flex items-center gap-2 py-2 text-xs text-[color:var(--paper-ink-3)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Loading experiment summary...
      </div>
    );
  }

  const scorePct = summary.avgScore != null ? summary.avgScore * 100 : null;
  // "Completion" is how many trials have finished — success, failed, AND
  // skipped are all terminal — matching the backend's done count
  // (resolve_task_status). The pass count lives in the Avg-score tile.
  const doneTrials =
    summary.completedTrials + summary.failedTrials + summary.skippedTrials;
  const completionPct =
    summary.totalTrials > 0 ? (doneTrials / summary.totalTrials) * 100 : 0;
  // Skipped is a terminal non-pass (its own bucket), so it belongs in the
  // outcome distribution alongside pass/partial/fail/harness — otherwise the
  // bar's percentages disagree with the pass metrics (e.g. 2 pass + 3 skipped
  // would read as 100% pass here while the pass rate is 2/5).
  const outcomeTotal =
    summary.passCount +
    summary.partialCount +
    summary.failCount +
    summary.harnessErrorCount +
    summary.skippedTrials;
  const outcomes = [
    ["pass", summary.passCount, "var(--paper-pass)"],
    ["partial", summary.partialCount, "var(--paper-partial)"],
    ["fail", summary.failCount, "var(--paper-fail)"],
    ["error", summary.harnessErrorCount, "var(--paper-error)"],
    ["skipped", summary.skippedTrials, "var(--paper-ink-3)"],
  ] as const;
  const costIsSpend = costStatus === "ready";
  const costPending = costStatus === "idle" || costStatus === "loading";
  const costUnavailable = costStatus === "error";

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <SummaryStat
          label="Avg score"
          description="Average of per-task average reward, nop/oracle excluded"
        >
          {isLoadingTrials ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : scorePct != null ? (
            `${scorePct.toFixed(1)}%`
          ) : (
            "—"
          )}
        </SummaryStat>
        <SummaryStat
          label="Trials finished"
          description="Trials that finished running, including failed and skipped trials. Download progress appears above the table."
          hint={
            <>
              {completionPct.toFixed(0)}%
              {summary.skippedTrials > 0 && (
                <> · {summary.skippedTrials} skipped</>
              )}
              {summary.failedTrials > 0 && (
                <span className="text-[color:var(--paper-fail)]">
                  {" "}
                  · {summary.failedTrials} failing
                </span>
              )}
            </>
          }
        >
          {doneTrials} / {summary.totalTrials}
        </SummaryStat>
        <SummaryStat label="Tasks">{taskCount}</SummaryStat>
        <SummaryStat
          label="Cost"
          description="Total cost of all trials shown in this experiment, including trials gathered from other experiments."
          hint={
            !costPending && !costUnavailable && summary.tokenTrialCount > 0
              ? formatTokenCount(summary.tokenCount)
              : undefined
          }
        >
          {costUnavailable ? (
            <span className="text-xs text-[color:var(--paper-fail)]">
              Unavailable
            </span>
          ) : (
            <CostValue
              cost={
                !costPending &&
                summary.costTrialCount > 0 &&
                hasDisplayableCostUsd(summary.costUsd)
                  ? summary.costUsd
                  : null
              }
              hasEstimated={summary.costHasEstimated}
              hasNative={summary.costHasNative}
              title={
                costPending
                  ? "Calculating experiment spend…"
                  : summary.costTrialCount > 0
                    ? `Summed across ${summary.costTrialCount} trial${summary.costTrialCount === 1 ? "" : "s"} shown in this experiment${
                        summary.costTrialCount > summary.ownedTrialCount
                          ? ", including trials gathered from other experiments (their spend is also reported there)"
                          : ""
                      }${costIsSpend ? ". The table shows only current-version trials" : ""}`
                    : "No cost data reported yet"
              }
            />
          )}
          {!costPending && !costUnavailable && (
            <QaCostSuffix
              costUsd={summary.qaCostUsd}
              title={
                summary.qaHasEstimated
                  ? "QA/analysis spend across this experiment's trials. Some values estimated from token counts × static model pricing. Not included in the cost figure."
                  : "QA/analysis spend across this experiment's trials. Not included in the cost figure."
              }
            />
          )}
        </SummaryStat>
        {showNewSpend && (
          <SummaryStat
            label="New spend"
            description="Spend from trials this experiment ran itself — excludes trials gathered from other experiments."
            hint={
              !costPending &&
              !costUnavailable &&
              summary.ownedTokenTrialCount > 0
                ? formatTokenCount(summary.ownedTokenCount)
                : undefined
            }
          >
            {costUnavailable ? (
              <span className="text-xs text-[color:var(--paper-fail)]">
                Unavailable
              </span>
            ) : (
              <CostValue
                cost={
                  costPending
                    ? null
                    : summary.ownedTrialCount > 0
                      ? summary.ownedCostUsd
                      : summary.ownedTokenTrialCount === 0 &&
                          summary.costTrialCount > 0
                        ? 0
                        : null
                }
                hasEstimated={
                  summary.ownedTrialCount > 0 && summary.ownedHasEstimated
                }
                hasNative={summary.ownedHasNative}
                title={
                  costPending
                    ? "Calculating new spend…"
                    : summary.ownedTrialCount > 0
                      ? `Summed across ${summary.ownedTrialCount} trial${summary.ownedTrialCount === 1 ? "" : "s"} this experiment ran itself${
                          summary.billedTrialCount > 0
                            ? `. ${formatCostUsd(summary.billedCostUsd)} of this was billed to user quotas`
                            : ". None of it was billed to a user quota"
                        }${costIsSpend ? ". The table shows only current-version trials" : ""}`
                      : summary.ownedTokenTrialCount > 0
                        ? "No cost data reported yet for this experiment's own trials"
                        : summary.costTrialCount > 0
                          ? "This experiment ran no trials of its own; every priced trial shown was gathered from another experiment, where its spend is reported."
                          : "No spend from this experiment yet"
                }
              />
            )}
            {!costPending && !costUnavailable && (
              <QaCostSuffix
                costUsd={summary.ownedQaCostUsd}
                title="QA/analysis spend on this experiment's own trials. Not included in the new spend figure."
              />
            )}
          </SummaryStat>
        )}
      </div>
      {qa && (
        <div className="border-t border-[color:var(--paper-line)] pt-2">
          <SummaryStat
            label="Verdicts"
            description="Task verdicts for the loaded tasks. Counts update as results arrive. Source audits, per-trial reviews, and human delivery sign-off are separate. Select a count to filter the results."
          >
            <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1 font-sans text-xs font-normal">
              {(
                [
                  ["accepted", qa.accepted, "Accepted"],
                  ["rejected", qa.rejected, "Rejected"],
                  ["running", qa.running, "Pending"],
                  ["failed", qa.failed, "Failed"],
                  ["unreviewed", qa.unreviewed, "No verdict"],
                ] as const
              )
                .filter(([, count]) => count > 0)
                .map(([value, count, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={reviewFilter === value}
                    className={`rounded px-1 py-0.5 text-left ${reviewFilter === value ? "bg-muted text-foreground" : "hover:bg-muted text-[color:var(--paper-ink-2)]"}`}
                    onClick={() =>
                      onReviewFilter(reviewFilter === value ? "all" : value)
                    }
                  >
                    {count} {label}
                  </button>
                ))}
              {reviewFilter !== "all" && (
                <button
                  className="underline"
                  onClick={() => onReviewFilter("all")}
                >
                  Show all tasks
                </button>
              )}
            </span>
          </SummaryStat>
        </div>
      )}
      <div
        className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-[color:var(--paper-ink-2)]"
        aria-label="Outcome distribution"
      >
        <div
          className="flex h-1.5 w-28 overflow-hidden rounded-[3px] bg-[color:var(--paper-bg-2)]"
          aria-hidden="true"
        >
          {outcomes.map(([label, count, color]) => (
            <span
              key={label}
              style={{
                width: `${outcomeTotal ? (count / outcomeTotal) * 100 : 0}%`,
                background: color,
              }}
            />
          ))}
        </div>
        {outcomes
          .filter(
            ([label, count]) =>
              count > 0 || label === "pass" || label === "fail"
          )
          .map(([label, count, color]) => (
            <span key={label} className="inline-flex items-center gap-1.5">
              <i
                className="inline-block h-2 w-2 rounded-[2px]"
                style={{ background: color }}
              />
              {count}{" "}
              <span className="text-[color:var(--paper-ink-3)]">{label}</span>
            </span>
          ))}
      </div>
    </div>
  );
}

export function ExperimentDetailView({
  experimentId,
  tasksForExperiment,
  pageSummary,
  costTotals,
  onRetryCostTotals,
  isLoading,
  isLoadingTrials = false,
  pagesComplete = true,
  hasError = false,
  errorTitle = "Failed to load experiment",
  errorDescription = "Check the API connection and try again.",
  headerLeft,
  headerStatus,
  headerRight,
  headerDescription,
  inlineAlert,
  readOnly = false,
  allowRetry = true,
  showAnalysis = true,
  apiBaseUrl = "/api",
  focusUrl,
  onTaskUnlink,
  onTrialDelete,
  onRerun,
  loadFullTrialOnOpen = false,
}: ExperimentDetailViewProps) {
  const searchParams = useSearchParams();
  // The experiment's own direct tags (the header editor chips); fetched
  // separately because no experiment payload carries them.
  const { data: experimentTags, mutate: mutateExperimentTags } = useSWR<
    UserTagRef[]
  >(
    experimentId
      ? `/api/tags/for-target?scope=EXPERIMENT&target_id=${encodeURIComponent(experimentId)}`
      : null,
    fetcher,
    { revalidateOnFocus: false }
  );
  const [drawerState, setDrawerState] = useState<DrawerState>(null);
  const rawReviewFilter = searchParams.get("verdict");
  const reviewFilter = [
    "accepted",
    "rejected",
    "running",
    "failed",
    "unreviewed",
  ].includes(rawReviewFilter ?? "")
    ? (rawReviewFilter as TaskReviewFilter)
    : "all";
  // Let Next copy its own history state; passing __NA bypasses hook updates.
  const setReviewFilter = useCallback((value: string) => {
    const params = new URLSearchParams(window.location.search);
    if (value === "all") params.delete("verdict");
    else params.set("verdict", value);
    window.history.pushState(null, "", urlWithSearch(params.toString()));
  }, []);
  const rejectedOnly = reviewFilter === "rejected";
  const setRejectedOnly = useCallback(
    (value: boolean) => setReviewFilter(value ? "rejected" : "all"),
    [setReviewFilter]
  );
  const reviewTasks = tasksForExperiment.filter((task) => {
    if (reviewFilter === "all" || reviewFilter === "rejected") return true;
    return taskReviewFilter(task) === reviewFilter;
  });
  // Task-definition pane addressing. The drawer can show the task's file
  // tree beside the trial view, so the two panes address independently:
  // the trial pane owns ?file= / ?lines= (see TrialDetailPanel) and the
  // task pane owns ?taskPane= / ?taskFile= / ?taskLines=.
  const defaultTaskPane: TaskPane = showAnalysis ? "overview" : "file";
  const readTaskPane = useCallback(
    (params: Pick<URLSearchParams, "get" | "has">): TaskPane => {
      const pane = params.get("taskPane");
      if (pane === "overview") return "overview";
      if (pane === "file") return "file";
      if (params.has("taskFile")) return "file";
      return defaultTaskPane;
    },
    [defaultTaskPane]
  );
  const [activeTaskPane, setActiveTaskPane] = useState<TaskPane>(() => {
    return readTaskPane(searchParams);
  });
  const selectTaskPane = useCallback((pane: TaskPane) => {
    setActiveTaskPane(pane);
    const params = new URLSearchParams(window.location.search);
    if (pane === "overview") params.delete("taskPane");
    else params.set("taskPane", pane);
    window.history.pushState(null, "", urlWithSearch(params.toString()));
  }, []);
  useEffect(() => {
    const restoreTaskPane = () => {
      const params = new URLSearchParams(window.location.search);
      setActiveTaskPane(readTaskPane(params));
    };
    window.addEventListener("popstate", restoreTaskPane);
    return () => window.removeEventListener("popstate", restoreTaskPane);
  }, [readTaskPane]);
  const [taskPaneFile, setTaskPaneFile] = useState<string | null>(() =>
    searchParams.get("taskFile")
  );
  const [taskPaneLines, setTaskPaneLines] = useState<LineRange | null>(() =>
    parseLineRange(searchParams.get("taskLines"))
  );
  // Mirrors taskPaneFile so the change handler can compare without an
  // impure setState updater.
  const taskPaneFileRef = useRef<string | null>(taskPaneFile);
  const handleTaskPaneFileChange = useCallback((path: string | null) => {
    // A different file makes the old line anchor meaningless — drop it.
    if (!sameFilePath(taskPaneFileRef.current, path)) setTaskPaneLines(null);
    taskPaneFileRef.current = path;
    setTaskPaneFile(path);
  }, []);
  // Reset the pane address when the drawer moves to another task (grid
  // selection, prev/next nav) or closes — the old path belongs to the old
  // task. The first open keeps it so deep links land.
  const lastDrawerTaskIdRef = useRef<string | null>(null);
  useEffect(() => {
    const taskId = drawerState?.task.id ?? null;
    if (
      lastDrawerTaskIdRef.current !== null &&
      taskId !== lastDrawerTaskIdRef.current
    ) {
      handleTaskPaneFileChange(null);
      setActiveTaskPane(defaultTaskPane);
    }
    lastDrawerTaskIdRef.current = taskId;
  }, [drawerState?.task.id, handleTaskPaneFileChange, defaultTaskPane]);
  // Probe cells open main's sliding ProbeDetailPanel (kept from origin/main).
  // On the slim experiment path the grid has no probe trials to click, so this
  // stays dormant until probes are fed to that path -- the code is retained so
  // main's probe-drawer feature is preserved and the merge stays coherent.
  const [probeDrawer, setProbeDrawer] = useState<{
    taskId: string;
    trialId: string;
  } | null>(null);
  const [showPassAtK, setShowPassAtK] = useState(readOnly);
  const drawerLayout = useUserUiLayout(!readOnly);
  // Incoming links reveal their target without changing the account's layout.
  // Capture only the incoming URL: drawer navigation also writes these params.
  const [linkedTaskPaneVisible, setLinkedTaskPaneVisible] = useState(
    () => searchParams.has("taskFile") || searchParams.has("taskPane")
  );
  const showTask = linkedTaskPaneVisible || drawerLayout.layout.showTask;
  const showTrial = drawerLayout.layout.showTrial;
  const handleShowTaskChange = (showTask: boolean) => {
    drawerLayout.update({ showTask, showTrial });
    setLinkedTaskPaneVisible(false);
    void drawerLayout.flush();
  };
  const handleShowTrialChange = (showTrial: boolean) => {
    drawerLayout.update({ showTask, showTrial });
    setLinkedTaskPaneVisible(false);
    void drawerLayout.flush();
  };
  const [cachedAgentSummaries, setCachedAgentSummaries] = useState<
    ExperimentAgentSummary[]
  >([]);
  const hydratedFromUrl = useRef(false);
  const [pendingUrlTaskSelector, setPendingUrlTaskSelector] = useState<
    string | null
  >(null);
  const [pendingUrlTrialId, setPendingUrlTrialId] = useState<string | null>(
    null
  );
  // Task drawer opened by hydration itself while a deep-link trial was still
  // pending (possibly from a stale ?task= naming the wrong task). The
  // resolver may replace this drawer; any other open drawer means the user
  // navigated, and the deep link yields.
  const hydrationTaskIdRef = useRef<string | null>(null);
  const focusQuery = useMemo(() => {
    if (!focusUrl || (!pendingUrlTaskSelector && !pendingUrlTrialId))
      return null;
    const query = new URLSearchParams();
    if (pendingUrlTaskSelector) query.set("task", pendingUrlTaskSelector);
    if (pendingUrlTrialId) query.set("trial", pendingUrlTrialId);
    return `${focusUrl}?${query}`;
  }, [focusUrl, pendingUrlTaskSelector, pendingUrlTrialId]);
  const { data: resolvedUrlFocus, error: urlFocusError } = useSWR<
    ExperimentFocusResponse | PublicExperimentFocusResponse
  >(focusQuery, fetcher, {
    revalidateOnFocus: false,
    shouldRetryOnError: isRetryableFocusError,
  });
  const hasPendingUrlFocus =
    pendingUrlTaskSelector != null || pendingUrlTrialId != null;
  const isInitialLoading = isLoading && tasksForExperiment.length === 0;
  const deferredTasksForDerivedData = useDeferredValue(tasksForExperiment);

  const agentSummaryStorageKey = experimentId
    ? `${AGENT_SUMMARY_STORAGE_PREFIX}${experimentId}`
    : null;
  const { agentSummaries, modelScopedAgents } = useMemo(
    () => buildExperimentAgentSummaries(deferredTasksForDerivedData),
    [deferredTasksForDerivedData]
  );
  const displayAgentSummaries =
    agentSummaries.length > 0 ? agentSummaries : cachedAgentSummaries;
  const displayModelScopedAgents = useMemo(
    () =>
      agentSummaries.length > 0
        ? modelScopedAgents
        : getModelScopedAgentsFromSummaries(cachedAgentSummaries),
    [agentSummaries, modelScopedAgents, cachedAgentSummaries]
  );

  useEffect(() => {
    if (!agentSummaryStorageKey) {
      setCachedAgentSummaries([]);
      return;
    }

    try {
      const raw = window.sessionStorage.getItem(agentSummaryStorageKey);
      if (!raw) {
        setCachedAgentSummaries([]);
        return;
      }
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        setCachedAgentSummaries(parsed as ExperimentAgentSummary[]);
      }
    } catch {
      setCachedAgentSummaries([]);
    }
  }, [agentSummaryStorageKey]);

  useEffect(() => {
    if (!agentSummaryStorageKey || agentSummaries.length === 0) return;
    setCachedAgentSummaries(agentSummaries);
    try {
      window.sessionStorage.setItem(
        agentSummaryStorageKey,
        JSON.stringify(agentSummaries)
      );
    } catch {
      // Ignore storage failures; the live data still drives the table.
    }
  }, [agentSummaryStorageKey, agentSummaries]);

  const buildTrialGroups = useCallback(
    (task: Task) => {
      const trialGroups: Array<{
        agent: string;
        model: string | null;
        trials: Trial[];
      }> = [];
      const trialsByAgent = new Map<string, Trial[]>();
      for (const trial of task.trials ?? []) {
        const key = getExperimentAgentKey(trial, displayModelScopedAgents);
        const existing = trialsByAgent.get(key) ?? [];
        existing.push(trial);
        trialsByAgent.set(key, existing);
      }
      for (const [key, trials] of trialsByAgent) {
        const model = trials.find((t) => t.model)?.model ?? null;
        trialGroups.push({
          agent: key,
          model,
          trials,
        });
      }
      const orderedTrials: Trial[] = [];
      for (const group of trialGroups) {
        orderedTrials.push(...group.trials);
      }
      return { trialGroups, orderedTrials };
    },
    [displayModelScopedAgents]
  );

  useEffect(() => {
    if (!hydratedFromUrl.current) return;

    // Base the rewrite on the live URL, not the useSearchParams snapshot:
    // replaceState never refreshes that hook, and TrialDetailPanel writes
    // its own params (tab/file/lines) the same way — a stale base here
    // would silently wipe them (and vice versa).
    const current = new URLSearchParams(window.location.search);
    const next = new URLSearchParams(window.location.search);
    if (drawerState?.isOpen) {
      next.set("task", drawerState.task.id);
      if (drawerState.mode === "trial" && drawerState.trial) {
        next.set("trial", drawerState.trial.id);
      } else if (!pendingUrlTrialId) {
        // Keep ?trial= while a deep link is still resolving from task mode.
        next.delete("trial");
      }
      if (activeTaskPane === "overview") {
        next.delete("taskPane");
      } else {
        next.set("taskPane", activeTaskPane);
      }
      if (activeTaskPane === "file" && taskPaneFile) {
        next.set("taskFile", taskPaneFile);
      } else {
        next.delete("taskFile");
      }
      if (activeTaskPane === "file" && taskPaneLines) {
        next.set("taskLines", formatLineRange(taskPaneLines));
      } else {
        next.delete("taskLines");
      }
    }

    if (next.toString() !== current.toString()) {
      const url = urlWithSearch(next.toString());
      // Keep URL query in sync without triggering app-router navigation work.
      window.history.replaceState(null, "", url);
    }
  }, [
    activeTaskPane,
    drawerState,
    hasPendingUrlFocus,
    pendingUrlTrialId,
    taskPaneFile,
    taskPaneLines,
  ]);

  useEffect(() => {
    if (hydratedFromUrl.current || tasksForExperiment.length === 0) return;
    hydratedFromUrl.current = true;

    const urlTaskId = searchParams.get("task");
    const trialParam = searchParams.get("trial");
    if (!urlTaskId && !trialParam) return;

    // Fall back to task name so hand-written links like ?task=<name> work;
    // the URL-sync effect rewrites the param to the canonical id on open.
    const task = urlTaskId
      ? (tasksForExperiment.find((t) => t.id === urlTaskId) ??
        tasksForExperiment.find((t) => t.name === urlTaskId))
      : null;

    // A hand-shortened ?trial= is an index against the task in the address;
    // the full id links carry passes through untouched.
    const urlTrialId = expandTrialParam(trialParam, task?.id ?? urlTaskId);

    if (urlTrialId) {
      // The trial id is the source of truth for its host task, so scan every
      // loaded task rather than trusting ?task= — a stale or missing task
      // param must not strand the link. Public share pages carry their full
      // trials here; the authed page usually has none yet and falls through
      // to the pending path.
      for (const host of tasksForExperiment) {
        const trial = (host.trials ?? []).find((t) => t.id === urlTrialId);
        if (trial) {
          const { trialGroups, orderedTrials } = buildTrialGroups(host);
          setDrawerState({
            isOpen: true,
            mode: "trial",
            task: host,
            taskIndex: tasksForExperiment.indexOf(host),
            orderedTasks: tasksForExperiment,
            taskNavScope: "experiment",
            trial,
            trialIndex: orderedTrials.findIndex((t) => t.id === trial.id),
            orderedTrials,
            trialGroups,
          });
          return;
        }
      }
      // Not loaded yet: keep the id pending; it resolves from a streamed
      // trial page or the experiment-scoped focus resource.
      setPendingUrlTrialId(urlTrialId);
      setPendingUrlTaskSelector(urlTaskId);
      hydrationTaskIdRef.current = task?.id ?? null;
    }

    if (!task) {
      if (urlTaskId) setPendingUrlTaskSelector(urlTaskId);
      return;
    }

    const taskIndex = tasksForExperiment.indexOf(task);
    const { trialGroups, orderedTrials } = buildTrialGroups(task);
    setDrawerState({
      isOpen: true,
      mode: "task",
      task,
      taskIndex,
      orderedTasks: tasksForExperiment,
      taskNavScope: "experiment",
      trial: null,
      trialIndex: null,
      orderedTrials,
      trialGroups,
    });
  }, [tasksForExperiment, searchParams, buildTrialGroups]);

  // Re-sync the open drawer with freshly-loaded trial data. On direct URL
  // loads the drawer opens as soon as the lightweight task shells arrive,
  // before trial pages stream in; without this, ``trialGroups`` stays empty
  // and the task↔trial nav row (``onNavigateToFirstTrial``) never appears.
  // Also handles the case where trials finish loading while a drawer opened
  // from a row click is already mounted.
  useEffect(() => {
    if (!drawerState) return;
    const liveTask = tasksForExperiment.find(
      (t) => t.id === drawerState.task.id
    );
    if (!liveTask) return;
    // Preserve open order, then append newly streamed tasks in scope so
    // next/prev grows with /open pages. Each review group stays in scope,
    // including dropping rows whose review status changed after a refresh.
    const liveById = new Map(
      tasksForExperiment.map((task) => [task.id, task] as const)
    );
    const remappedOrderedTasks = drawerState.orderedTasks
      .map((task) => liveById.get(task.id))
      .filter((task): task is Task => task != null);
    const preservedOrderedTasks =
      drawerState.taskNavScope !== "experiment"
        ? remappedOrderedTasks.filter(
            (task) => taskReviewFilter(task) === drawerState.taskNavScope
          )
        : remappedOrderedTasks;
    const seen = new Set(preservedOrderedTasks.map((task) => task.id));
    const growthPool =
      drawerState.taskNavScope !== "experiment"
        ? tasksForExperiment.filter(
            (task) => taskReviewFilter(task) === drawerState.taskNavScope
          )
        : tasksForExperiment;
    const scopedOrderedTasks = [
      ...preservedOrderedTasks,
      ...growthPool.filter((task) => !seen.has(task.id)),
    ];
    // An empty review group must leave its scope before falling back to the
    // experiment list, or the next render would remove those rows again.
    const leaveReviewNav =
      drawerState.taskNavScope !== "experiment" &&
      scopedOrderedTasks.length === 0;
    const orderedTasks = leaveReviewNav
      ? tasksForExperiment
      : scopedOrderedTasks.length > 0
        ? scopedOrderedTasks
        : tasksForExperiment;
    const taskNavScope = leaveReviewNav
      ? "experiment"
      : drawerState.taskNavScope;
    let nextTask = liveTask;
    let resolvedTaskIndex = orderedTasks.findIndex(
      (task) => task.id === liveTask.id
    );
    if (resolvedTaskIndex < 0 && orderedTasks.length > 0) {
      // Open task left the nav set (e.g. no longer rejected); snap so
      // taskIndex and the visible task stay aligned for next/prev.
      resolvedTaskIndex = Math.min(
        drawerState.taskIndex,
        orderedTasks.length - 1
      );
      nextTask = orderedTasks[resolvedTaskIndex]!;
    } else if (resolvedTaskIndex < 0) {
      resolvedTaskIndex = 0;
    }
    const orderedChanged =
      orderedTasks.length !== drawerState.orderedTasks.length ||
      taskNavScope !== drawerState.taskNavScope ||
      nextTask.id !== drawerState.task.id ||
      orderedTasks.some(
        (task, index) => task !== drawerState.orderedTasks[index]
      );
    const liveTrialCount = liveTask.trials?.length ?? 0;
    const snapshotTrialCount = drawerState.task.trials?.length ?? 0;
    if (
      liveTask === drawerState.task &&
      nextTask.id === drawerState.task.id &&
      liveTrialCount === snapshotTrialCount &&
      !orderedChanged
    ) {
      return;
    }
    const { trialGroups, orderedTrials } = buildTrialGroups(nextTask);
    const foundTrialIndex = drawerState.trial
      ? orderedTrials.findIndex((t) => t.id === drawerState.trial!.id)
      : -1;
    const resolvedTrialIndex = foundTrialIndex >= 0 ? foundTrialIndex : null;
    const resolvedTrial =
      resolvedTrialIndex != null
        ? orderedTrials[resolvedTrialIndex]
        : nextTask.id === drawerState.task.id
          ? drawerState.trial
          : null;
    const snappedAway = nextTask.id !== drawerState.task.id;
    if (leaveReviewNav && reviewFilter === drawerState.taskNavScope) {
      setReviewFilter("all");
    }
    setDrawerState({
      ...drawerState,
      mode: snappedAway && resolvedTrial == null ? "task" : drawerState.mode,
      task: nextTask,
      taskIndex: resolvedTaskIndex,
      orderedTasks,
      taskNavScope,
      trial: resolvedTrial,
      trialIndex: resolvedTrialIndex,
      orderedTrials,
      trialGroups,
    });
  }, [
    tasksForExperiment,
    drawerState,
    buildTrialGroups,
    reviewFilter,
    setReviewFilter,
  ]);

  const clearPendingDeepLink = useCallback(() => {
    setPendingUrlTaskSelector(null);
    setPendingUrlTrialId(null);
    hydrationTaskIdRef.current = null;
  }, []);

  // Any drawer change the user makes themselves cancels an unresolved deep
  // link: a late resolve must never yank them away from where they went.
  const cancelPendingDeepLink = useCallback(() => {
    clearPendingDeepLink();
    setLinkedTaskPaneVisible(false);
    const current = new URLSearchParams(window.location.search);
    const next = new URLSearchParams(window.location.search);
    next.delete("task");
    next.delete("trial");
    next.delete("tab");
    next.delete("file");
    next.delete("lines");
    next.delete("taskFile");
    next.delete("taskLines");
    next.delete("taskPane");
    if (next.toString() !== current.toString()) {
      window.history.replaceState(null, "", urlWithSearch(next.toString()));
    }
  }, [clearPendingDeepLink]);

  // Open a resolved deep-link trial. Yields if the user has navigated on
  // their own since hydration: only a closed drawer, the host task's own
  // task-mode drawer, or the task drawer hydration itself opened (possibly
  // off a stale ?task=) may be replaced. Yielding still clears the pending
  // state — a deep link never overrides the user.
  const openDeepLinkTrial = useCallback(
    (host: Task, trial: Trial) => {
      if (
        drawerState &&
        !(
          drawerState.mode === "task" &&
          (drawerState.task.id === host.id ||
            drawerState.task.id === hydrationTaskIdRef.current)
        )
      ) {
        clearPendingDeepLink();
        return;
      }
      const { trialGroups, orderedTrials } = buildTrialGroups(host);
      const index = orderedTrials.findIndex((item) => item.id === trial.id);
      setDrawerState({
        isOpen: true,
        mode: "trial",
        task: host,
        taskIndex: tasksForExperiment.findIndex((task) => task.id === host.id),
        orderedTasks: tasksForExperiment,
        taskNavScope: "experiment",
        trial: index >= 0 ? orderedTrials[index] : trial,
        trialIndex: index >= 0 ? index : null,
        orderedTrials,
        trialGroups,
      });
      const next = new URLSearchParams(window.location.search);
      next.set("task", host.id);
      next.set("trial", trial.id);
      // This only canonicalizes drawer state in the URL. A route navigation
      // can suspend the whole experiment and reset its loaded table.
      window.history.replaceState(null, "", urlWithSearch(next.toString()));
      clearPendingDeepLink();
    },
    [drawerState, tasksForExperiment, buildTrialGroups, clearPendingDeepLink]
  );

  // The trial page can satisfy a pending URL before the focused read returns.
  useEffect(() => {
    if (pendingUrlTrialId == null) return;
    for (const host of tasksForExperiment) {
      const trial = (host.trials ?? []).find((t) => t.id === pendingUrlTrialId);
      if (trial) {
        openDeepLinkTrial(host, trial);
        return;
      }
    }
  }, [pendingUrlTrialId, tasksForExperiment, openDeepLinkTrial]);

  // The focused resource resolves one task and optional trial inside this
  // experiment, independent of the task and trial pagination cursors.
  useEffect(() => {
    if (!hasPendingUrlFocus) return;
    if (urlFocusError) {
      if (!isRetryableFocusError(urlFocusError)) cancelPendingDeepLink();
      return;
    }
    if (!resolvedUrlFocus || tasksForExperiment.length === 0) return;

    const focusedTrial = resolvedUrlFocus.trial
      ? trialFromExperimentCell(resolvedUrlFocus.trial)
      : null;
    const loadedTask = tasksForExperiment.find(
      (task) => task.id === resolvedUrlFocus.task.id
    );
    const existingTrials = loadedTask?.trials ?? [];
    const trials =
      focusedTrial &&
      !existingTrials.some((trial) => trial.id === focusedTrial.id)
        ? [...existingTrials, focusedTrial]
        : (loadedTask?.trials ?? (focusedTrial ? [focusedTrial] : undefined));
    const experiment = tasksForExperiment[0];
    const host: Task = {
      ...resolvedUrlFocus.task,
      experiment_id: experiment.experiment_id,
      experiment_name: experiment.experiment_name,
      experiment_is_public: experiment.experiment_is_public,
      experiment_created_at: experiment.experiment_created_at,
      experiment_owner: experiment.experiment_owner,
      experiment_link: experiment.experiment_link,
      ...loadedTask,
      trials,
    };

    if (focusedTrial) {
      openDeepLinkTrial(host, focusedTrial);
      return;
    }
    if (
      drawerState &&
      drawerState.task.id !== host.id &&
      drawerState.task.id !== hydrationTaskIdRef.current
    ) {
      clearPendingDeepLink();
      return;
    }
    const { trialGroups, orderedTrials } = buildTrialGroups(host);
    setDrawerState({
      isOpen: true,
      mode: "task",
      task: host,
      taskIndex: tasksForExperiment.findIndex((task) => task.id === host.id),
      orderedTasks: tasksForExperiment,
      taskNavScope: "experiment",
      trial: null,
      trialIndex: null,
      orderedTrials,
      trialGroups,
    });
    clearPendingDeepLink();
  }, [
    resolvedUrlFocus,
    urlFocusError,
    hasPendingUrlFocus,
    drawerState,
    tasksForExperiment,
    openDeepLinkTrial,
    buildTrialGroups,
    cancelPendingDeepLink,
    clearPendingDeepLink,
  ]);

  // The bounded open resource owns exact non-cost totals. The separate cost
  // resource owns whole-experiment spend because trial pages are incomplete
  // until pagination finishes.
  const exactCostTotals =
    costTotals.status === "ready" ? costTotals.data : undefined;
  const summary = useMemo(() => {
    const visible = buildExperimentSummary(deferredTasksForDerivedData);
    const base = pageSummary
      ? {
          ...visible,
          rewardSuccess: pageSummary.pass_count,
          rewardSum: pageSummary.reward_sum,
          rewardTotal: pageSummary.reward_total,
          avgScore: pageSummary.average_score,
          totalTrials: pageSummary.trial_count,
          completedTrials: pageSummary.completed,
          failedTrials: pageSummary.failed,
          skippedTrials: pageSummary.skipped,
          passCount: pageSummary.pass_count,
          partialCount: pageSummary.partial_count,
          failCount: pageSummary.fail_count,
          harnessErrorCount: pageSummary.harness_error_count,
          pendingCount: pageSummary.active,
        }
      : visible;
    if (!exactCostTotals) return base;
    return {
      ...base,
      costUsd: exactCostTotals.cost_usd,
      costTrialCount: exactCostTotals.cost_trial_count,
      costHasEstimated: exactCostTotals.cost_has_estimated,
      costHasNative: exactCostTotals.cost_has_native,
      qaCostUsd: exactCostTotals.qa_cost_usd ?? 0,
      ownedQaCostUsd: exactCostTotals.owned_qa_cost_usd ?? 0,
      qaHasEstimated: exactCostTotals.qa_has_estimated ?? false,
      tokenCount: exactCostTotals.token_count,
      tokenTrialCount: exactCostTotals.token_trial_count,
      // ?? base.*: deploy-skew guard — a backend that predates owned_* omits
      // the fields; the client fold's partial owned sum beats a hard $0.00.
      ownedCostUsd: exactCostTotals.owned_cost_usd ?? base.ownedCostUsd,
      ownedTrialCount:
        exactCostTotals.owned_trial_count ?? base.ownedTrialCount,
      ownedHasEstimated:
        exactCostTotals.owned_has_estimated ?? base.ownedHasEstimated,
      ownedHasNative: exactCostTotals.owned_has_native ?? base.ownedHasNative,
      ownedTokenCount:
        exactCostTotals.owned_token_count ?? base.ownedTokenCount,
      ownedTokenTrialCount:
        exactCostTotals.owned_token_trial_count ?? base.ownedTokenTrialCount,
      billedCostUsd: exactCostTotals.billed_cost_usd,
      billedTrialCount: exactCostTotals.billed_trial_count,
      billedHasEstimated: exactCostTotals.billed_has_estimated,
      billedHasNative: exactCostTotals.billed_has_native,
      billedTokenCount: exactCostTotals.billed_token_count,
      billedTokenTrialCount: exactCostTotals.billed_token_trial_count,
    };
  }, [deferredTasksForDerivedData, pageSummary, exactCostTotals]);

  // Count the same rows with the same classifier the review filters use.
  // The server summary does not include the live analysis carried by trials.
  const qaRollup = useMemo(() => {
    if (tasksForExperiment.length === 0) return null;
    const counts = {
      accepted: 0,
      rejected: 0,
      running: 0,
      failed: 0,
      unreviewed: 0,
    };
    for (const task of tasksForExperiment) counts[taskReviewFilter(task)] += 1;
    return counts;
  }, [tasksForExperiment]);

  const closeDrawer = () => {
    cancelPendingDeepLink();
    setDrawerState(null);
  };

  const handleNavigateToFirstTrial = () => {
    if (!drawerState) return;
    const firstGroup = drawerState.trialGroups[0];
    if (!firstGroup || firstGroup.trials.length === 0) return;

    cancelPendingDeepLink();
    const firstTrial = firstGroup.trials[0];
    setDrawerState({
      ...drawerState,
      mode: "trial",
      trial: firstTrial,
      trialIndex: 0,
    });
  };

  const handleNavigateToTask = () => {
    if (!drawerState) return;
    cancelPendingDeepLink();
    setDrawerState({
      ...drawerState,
      mode: "task",
      trial: null,
      trialIndex: null,
    });
  };

  const handleNavigateToTrial = (trial: Trial, trialIndex: number | null) => {
    if (!drawerState) return;
    cancelPendingDeepLink();
    setDrawerState({
      ...drawerState,
      mode: "trial",
      trial,
      trialIndex,
    });
  };

  // A trial link from the task overview's aggregated QA. Always opens in
  // this drawer: the overview hands over the full trial row, so even a
  // trial the grid hasn't streamed in yet (or one gathered from another
  // experiment) renders in place — never a navigation away. A grid match
  // is still preferred so the per-group trial nav lines up.
  const handleOpenTrialFromOverview = useCallback(
    (trial: Trial): boolean => {
      if (!drawerState) return false;
      const { trialGroups, orderedTrials } = buildTrialGroups(drawerState.task);
      const trialIndex = orderedTrials.findIndex((t) => t.id === trial.id);
      cancelPendingDeepLink();
      setDrawerState({
        ...drawerState,
        mode: "trial",
        trial: trialIndex >= 0 ? orderedTrials[trialIndex] : trial,
        trialIndex: trialIndex >= 0 ? trialIndex : null,
        orderedTrials,
        trialGroups,
      });
      return true;
    },
    [drawerState, buildTrialGroups, cancelPendingDeepLink]
  );

  return (
    <>
      {isInitialLoading ? (
        <ExperimentPageSkeleton />
      ) : (
        <div className="space-y-4">
          {/*
           * Experiment page header — editorial layout, no surrounding box.
           * Fraunces display title, a dot-separated meta strip, with the
           * Show-graph + Publish actions parked top-right.
           */}
          <div className="space-y-2">
            <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  {headerLeft}
                  {experimentId && (
                    <TagEditor
                      scope="EXPERIMENT"
                      targetId={experimentId}
                      initialTags={experimentTags ?? []}
                      experimentMode="living"
                      onMutate={() => void mutateExperimentTags()}
                    />
                  )}
                </div>
                <ExperimentMetaStrip
                  tasks={tasksForExperiment}
                  isInitialLoading={isInitialLoading}
                  experimentId={experimentId}
                  readOnly={readOnly}
                />
              </div>
              <ExperimentHeaderMeta
                isLoading={isLoading}
                isInitialLoading={isInitialLoading}
                headerStatus={headerStatus}
                showPassAtK={showPassAtK}
                onToggleShowPassAtK={() => setShowPassAtK((prev) => !prev)}
                headerRight={headerRight}
                prLink={
                  // The PR chip links into GitHub for the experiment's source
                  // branch — internal context that shouldn't surface on the
                  // public share view.
                  readOnly ? undefined : (
                    <ExperimentPrLink
                      tasks={tasksForExperiment}
                      isInitialLoading={isInitialLoading}
                    />
                  )
                }
              />
            </div>

            {headerDescription}
          </div>

          <ExperimentSummaryBar
            taskCount={pageSummary?.task_count ?? tasksForExperiment.length}
            summary={summary}
            isInitialLoading={isInitialLoading}
            isLoadingTrials={isLoadingTrials}
            // The owned-vs-gathered spend split (and the billing attribution
            // in its tooltip) is internal; keep it off the public share view
            // (the only readOnly consumer).
            showNewSpend={!readOnly}
            costStatus={costTotals.status}
            qa={showAnalysis ? qaRollup : null}
            reviewFilter={reviewFilter}
            onReviewFilter={setReviewFilter}
          />

          {!hasError && costTotals.status === "error" && (
            <Alert variant="destructive">
              <AlertTitle>Failed to load experiment spend</AlertTitle>
              <AlertDescription className="flex flex-wrap items-center gap-2">
                <span>{costTotals.message}</span>
                <span>Exact cost and token totals are unavailable.</span>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  className="h-7"
                  onClick={onRetryCostTotals}
                  disabled={costTotals.isRetrying}
                >
                  {costTotals.isRetrying ? "Retrying…" : "Retry"}
                </Button>
              </AlertDescription>
            </Alert>
          )}

          {hasError ? (
            (inlineAlert ?? (
              <Alert variant="destructive">
                <AlertTitle>{errorTitle}</AlertTitle>
                <AlertDescription>{errorDescription}</AlertDescription>
              </Alert>
            ))
          ) : (
            <div className="space-y-3">
              {inlineAlert}
              <ExperimentTrialsTable
                tasks={reviewTasks}
                agentSummaries={displayAgentSummaries}
                modelScopedAgents={displayModelScopedAgents}
                isLoading={isLoading}
                isLoadingTrials={isLoadingTrials}
                pagesComplete={pagesComplete}
                showPassAtK={showPassAtK}
                experimentId={experimentId}
                onTaskUnlink={onTaskUnlink}
                onRerun={onRerun}
                allowRerun={allowRetry}
                readOnly={readOnly}
                showAnalysis={showAnalysis}
                rejectedOnly={rejectedOnly}
                onRejectedOnlyChange={setRejectedOnly}
                onTrialSelect={(trial, task, context) => {
                  cancelPendingDeepLink();
                  setDrawerState({
                    isOpen: true,
                    mode: "trial",
                    task,
                    taskIndex: context.taskIndex,
                    orderedTasks: context.orderedTasks,
                    taskNavScope:
                      reviewFilter === "all"
                        ? (context.taskNavScope ?? "experiment")
                        : reviewFilter,
                    trial,
                    trialIndex: context.trialIndex,
                    orderedTrials: context.orderedTrials,
                    trialGroups: context.trialGroups,
                  });
                }}
                onProbeSelect={(trial, task) => {
                  cancelPendingDeepLink();
                  setProbeDrawer({ taskId: task.id, trialId: trial.id });
                }}
                onTaskSelect={(task, context) => {
                  cancelPendingDeepLink();
                  const { trialGroups, orderedTrials } = buildTrialGroups(task);
                  // Land on the task overview — task-level QA plus the
                  // aggregated trial QA — never on a specific trial. Trials
                  // are one click away via "View trials" or the overview's
                  // per-trial links.
                  setDrawerState({
                    isOpen: true,
                    mode: "task",
                    task,
                    taskIndex: context.taskIndex,
                    orderedTasks: context.orderedTasks,
                    taskNavScope:
                      reviewFilter === "all"
                        ? (context.taskNavScope ?? "experiment")
                        : reviewFilter,
                    trial: null,
                    trialIndex: null,
                    orderedTrials,
                    trialGroups,
                  });
                }}
                onTaskNavChange={({ orderedTasks, taskNavScope }) => {
                  setDrawerState((prev) => {
                    if (!prev) return prev;
                    const liveById = new Map(
                      orderedTasks.map((task) => [task.id, task] as const)
                    );
                    const task =
                      liveById.get(prev.task.id) ??
                      tasksForExperiment.find((t) => t.id === prev.task.id) ??
                      prev.task;
                    const taskIndex = orderedTasks.findIndex(
                      (candidate) => candidate.id === task.id
                    );
                    return {
                      ...prev,
                      task,
                      taskIndex: taskIndex >= 0 ? taskIndex : prev.taskIndex,
                      orderedTasks,
                      taskNavScope,
                    };
                  });
                }}
              />
            </div>
          )}
        </div>
      )}

      {drawerState && (
        <UnifiedDrawerWrapper
          key={drawerLayout.identity ?? "public"}
          layout={drawerLayout.layout}
          onLayoutChange={drawerLayout.update}
          onLayoutCommit={drawerLayout.flush}
          layoutSaveError={drawerLayout.status === "error"}
          onRetryLayoutSave={drawerLayout.retry}
          open={drawerState.isOpen}
          onOpenChange={(open) => {
            void drawerLayout.flush();
            if (!open) closeDrawer();
          }}
          mode={drawerState.mode}
          showTask={showTask}
          showTrial={showTrial}
          onShowTaskChange={handleShowTaskChange}
          onShowTrialChange={handleShowTrialChange}
          sideBySideLeft={
            <TaskFilesPanel
              // This tells the panel whether the pane is actually visible
              // on screen. The panel starts downloading its files when
              // isOpen becomes true, so passing a hardcoded true would
              // download files for a pane the user cannot see.
              isOpen={drawerState.mode === "trial" && showTask}
              onClose={() => {}}
              activePane={activeTaskPane}
              onActivePaneChange={selectTaskPane}
              taskId={null}
              // The task prop scopes the overview's trial aggregation to
              // this experiment's rows; this pane renders no header, so
              // none of the task-driven header UI appears.
              task={drawerState.task}
              staticChecksTaskId={drawerState.task.id}
              onOpenTrial={handleOpenTrialFromOverview}
              overviewTrialsLoading={isLoadingTrials}
              filesUrl={`${apiBaseUrl}/tasks/${drawerState.task.id}/files`}
              taskVersion={resolveExperimentTaskVersion(drawerState.task)}
              initialFilePath={taskPaneFile}
              selectedLines={taskPaneLines}
              onSelectLinesChange={setTaskPaneLines}
              onSelectedFileChange={handleTaskPaneFileChange}
              apiBaseUrl={apiBaseUrl}
              cancelExperimentId={experimentId}
              showAnalysis={showAnalysis}
              loadFilesLazily
              contentOnly={true}
            />
          }
          taskContent={
            <TaskFilesPanel
              isOpen={drawerState.mode === "task"}
              onClose={closeDrawer}
              activePane={activeTaskPane}
              onActivePaneChange={selectTaskPane}
              taskId={drawerState.task.id}
              task={drawerState.task}
              taskVersion={resolveExperimentTaskVersion(drawerState.task)}
              orderedTasks={drawerState.orderedTasks}
              taskIndex={drawerState.taskIndex}
              onRetryComplete={onRerun}
              allowRetry={allowRetry}
              cancelExperimentId={experimentId}
              showAnalysis={showAnalysis}
              loadFilesLazily
              onNavigate={(nextTask, nextIndex) => {
                if (!drawerState) return;
                cancelPendingDeepLink();
                const { trialGroups, orderedTrials } =
                  buildTrialGroups(nextTask);
                setDrawerState({
                  ...drawerState,
                  task: nextTask,
                  taskIndex: nextIndex,
                  orderedTrials,
                  trialGroups,
                });
              }}
              onNavigateToFirstTrial={
                drawerState.trialGroups.length > 0
                  ? handleNavigateToFirstTrial
                  : undefined
              }
              onOpenTrial={handleOpenTrialFromOverview}
              overviewTrialsLoading={isLoadingTrials}
              initialFilePath={taskPaneFile}
              selectedLines={taskPaneLines}
              onSelectLinesChange={setTaskPaneLines}
              onSelectedFileChange={handleTaskPaneFileChange}
              apiBaseUrl={apiBaseUrl}
              contentOnly={true}
            />
          }
          renderTrial={(paneAction) =>
            drawerState.trial && (
              <TrialDetailPanel
                isOpen={true}
                onClose={closeDrawer}
                trial={drawerState.trial}
                task={drawerState.task}
                orderedTrials={drawerState.orderedTrials}
                trialIndex={drawerState.trialIndex}
                trialGroups={drawerState.trialGroups}
                onNavigate={handleNavigateToTrial}
                onNavigateToTask={handleNavigateToTask}
                onRetry={onRerun}
                onDelete={onTrialDelete}
                allowRetry={allowRetry}
                showAnalysis={showAnalysis}
                requireTrialDetail={loadFullTrialOnOpen}
                allowDelete={Boolean(onTrialDelete)}
                apiBaseUrl={apiBaseUrl}
                contentOnly={true}
                paneAction={paneAction}
              />
            )
          }
        />
      )}
      {probeDrawer && (
        <ProbeDetailPanel
          taskId={probeDrawer.taskId}
          trialId={probeDrawer.trialId}
          isOpen
          onClose={() => setProbeDrawer(null)}
        />
      )}
    </>
  );
}
