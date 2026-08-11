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
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ExperimentTrialsTable } from "@/components/experiment-trials-table";
import { QaCostSuffix } from "@/components/qa-cost-suffix";
import { TagEditor } from "@/components/tag-editor";
import { UnifiedDrawerWrapper } from "@/components/unified-drawer-wrapper";
import { fetcher } from "@/lib/api";
import { prBadge, prNumberFromUrl, taskPrUrl } from "@/lib/utils";
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
  ExperimentCostTotals,
  Task,
  Trial,
  UserTagRef,
} from "@/lib/types";
import { ExternalLink, GitPullRequest, Info, Loader2 } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  buildExperimentAgentSummaries,
  getExperimentAgentKey,
  isBaselineAgentName,
  type ExperimentAgentSummary,
} from "@/lib/experiment-agent-grouping";
import { resolveExperimentTaskVersion } from "@/lib/experiment-task-version";
import {
  formatLineRange,
  parseLineRange,
  type LineRange,
} from "@/lib/line-range";
import { sameFilePath } from "@/lib/file-path";
import { expandTrialParam, shortTrialParam } from "@/lib/trial-url";

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

type DrawerState = {
  isOpen: boolean;
  mode: DrawerMode;
  task: Task;
  taskIndex: number;
  orderedTasks: Task[];
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
  // Server-side spend rollup for the whole experiment. Omit (as the public
  // share view does) to fall back to summing the loaded trials, which
  // understates cost while pages are unloaded.
  costTotals?: ExperimentCostTotals;
  // True while the rollup is still in flight, so the cost tiles show a
  // placeholder instead of the (wrong) client sum. See experiment-client.
  costTotalsPending?: boolean;
  isLoading: boolean;
  isLoadingTrials?: boolean;
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
  onTaskUnlink?: (task: Task) => Promise<void>;
  onTrialDelete?: (trial: Trial, task: Task | null) => Promise<void>;
  onRerun?: (taskIds?: string[]) => void;
  // Logged-in exp page sends slim trials, so set true to fetch a clicked
  // trial's full detail on open. Public share page omits it (it passes full
  // trials and can't use the authed /api/trials route).
  loadFullTrialOnOpen?: boolean;
}

const AGENT_SUMMARY_STORAGE_PREFIX = "oddish:experiment-agent-summaries:";

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
    costUsd: acc.costUsd,
    costTrialCount: acc.costTrialCount,
    costHasEstimated: acc.costHasEstimated,
    costHasNative: acc.costHasNative,
    // QA has no client-side fold -- it rides in only via the server rollup
    // (the ``costTotals`` override below), so the base value is always zero.
    qaCostUsd: 0,
    ownedQaCostUsd: 0,
    qaHasEstimated: false,
    ownedCostUsd: acc.ownedCostUsd,
    ownedTrialCount: acc.ownedTrialCount,
    ownedHasEstimated: acc.ownedHasEstimated,
    ownedHasNative: acc.ownedHasNative,
    tokenCount: acc.tokenCount,
    tokenTrialCount: acc.tokenTrialCount,
    ownedTokenCount: acc.ownedTokenCount,
    ownedTokenTrialCount: acc.ownedTokenTrialCount,
    billedCostUsd: acc.billedCostUsd,
    billedTrialCount: acc.billedTrialCount,
    billedHasEstimated: acc.billedHasEstimated,
    billedHasNative: acc.billedHasNative,
    billedTokenCount: acc.billedTokenCount,
    billedTokenTrialCount: acc.billedTokenTrialCount,
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

function pickExperimentCreationMeta(tasks: Task[]): {
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
  const experimentOwner =
    tasks.find((task) => task.experiment_owner)?.experiment_owner ?? null;
  return {
    createdAt: experimentCreatedAt ?? earliest.created_at,
    author:
      experimentOwner ?? earliest.github_username ?? earliest.user ?? null,
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
  const { createdAt, author } = pickExperimentCreationMeta(tasks);
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

function KpiTile({
  label,
  labelInfo,
  children,
  className = "",
}: {
  label: string;
  labelInfo?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex flex-col gap-1.5 border-r border-[color:var(--paper-line-2)] px-4 py-3 last:border-r-0 ${className}`}
    >
      <span className="inline-flex items-center gap-1 font-mono text-[10px] font-semibold tracking-[0.09em] text-[color:var(--paper-ink-3)] uppercase">
        {label}
        {labelInfo && (
          <TooltipProvider delayDuration={150}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Info
                  className="h-3 w-3 cursor-help text-[color:var(--paper-ink-3)]"
                  aria-label={`How ${label} is calculated`}
                />
              </TooltipTrigger>
              <TooltipContent className="max-w-xs normal-case">
                {labelInfo}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </span>
      {children}
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
  costIsSpend,
  costPending,
}: {
  taskCount: number;
  summary: ExperimentSummary;
  isInitialLoading: boolean;
  isLoadingTrials: boolean;
  showNewSpend: boolean;
  costIsSpend: boolean;
  costPending: boolean;
}) {
  if (isInitialLoading) {
    return (
      <div className="flex items-center gap-2 rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3 text-xs text-[color:var(--paper-ink-3)]">
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
  const passPct = outcomeTotal ? (summary.passCount / outcomeTotal) * 100 : 0;
  const partialPct = outcomeTotal
    ? (summary.partialCount / outcomeTotal) * 100
    : 0;
  const failPct = outcomeTotal ? (summary.failCount / outcomeTotal) * 100 : 0;
  const errPct = outcomeTotal
    ? (summary.harnessErrorCount / outcomeTotal) * 100
    : 0;
  const skippedPct = outcomeTotal
    ? (summary.skippedTrials / outcomeTotal) * 100
    : 0;

  return (
    <div
      className={`grid grid-cols-2 overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] ${
        showNewSpend
          ? "md:grid-cols-[1.1fr_1fr_0.9fr_0.9fr_0.9fr_1.4fr]"
          : "md:grid-cols-[1.1fr_1fr_0.9fr_0.9fr_1.4fr]"
      }`}
    >
      <KpiTile
        label="Avg score"
        labelInfo="Average of per-task average reward, nop/oracle excluded"
      >
        <span className="font-display flex items-baseline gap-2 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
          {isLoadingTrials ? (
            // The score is computed from streamed trial pages; rendering an
            // intermediate value would show a number that jumps once the
            // remaining pages land.
            <Loader2 className="h-5 w-5 animate-spin text-[color:var(--paper-ink-3)]" />
          ) : scorePct != null ? (
            `${scorePct.toFixed(1)}%`
          ) : (
            "—"
          )}
        </span>
      </KpiTile>
      <KpiTile label="Completion">
        <span className="font-display flex items-baseline gap-2 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
          {doneTrials}
          <span className="font-mono text-xs font-normal text-[color:var(--paper-ink-3)]">
            / {summary.totalTrials} trials
          </span>
        </span>
        <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
          {completionPct.toFixed(0)}%
          {summary.skippedTrials > 0 && (
            <span className="ml-1.5 text-[color:var(--paper-ink-3)]">
              · {summary.skippedTrials} skipped
            </span>
          )}
          {summary.failedTrials > 0 && (
            <span className="ml-1.5 text-[color:var(--paper-fail)]">
              · {summary.failedTrials} failing
            </span>
          )}
        </span>
      </KpiTile>
      <KpiTile label="Tasks">
        <span className="font-display flex items-baseline gap-2 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
          {taskCount}
          <span className="font-mono text-xs font-normal text-[color:var(--paper-ink-3)]">
            tasks
          </span>
        </span>
      </KpiTile>
      <KpiTile
        label="Cost"
        labelInfo="Total cost of all trials shown in this experiment, including trials gathered from other experiments."
      >
        <span
          className="font-display flex items-baseline gap-1 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]"
          title={
            // Must agree with the VALUE rendered below: while the rollup is in
            // flight the tile shows an em dash, so the tooltip cannot describe
            // the client fold's (partial, grid-scoped) counts.
            costPending
              ? "Calculating experiment spend…"
              : summary.costTrialCount > 0
                ? `Summed across ${summary.costTrialCount} trial${
                    summary.costTrialCount === 1 ? "" : "s"
                  } shown in this experiment${
                    // Gathered/shared-task spend is deliberately included: it
                    // prices the work on this page. Warn that those dollars
                    // are also reported on their home experiments so nobody
                    // sums Cost tiles across pages.
                    summary.costTrialCount > summary.ownedTrialCount
                      ? ", including trials gathered from other experiments (their spend is also reported there)"
                      : ""
                  }${
                    // Spend covers every trial that ran; the table is filtered to
                    // each task's current version. Say so, or the tile reads as
                    // "wrong" whenever a task was re-uploaded or a trial retried.
                    costIsSpend
                      ? ". The table shows only current-version trials"
                      : ""
                  }${
                    summary.costHasEstimated && summary.costHasNative
                      ? ". Mixed native + estimated values; ~ marks estimates."
                      : summary.costHasEstimated
                        ? ". Estimated from token counts × static model pricing."
                        : ". Reported by the agent runtime."
                  }`
                : "No cost data reported yet"
          }
        >
          {costPending ? (
            <span className="text-[color:var(--paper-ink-3)]">—</span>
          ) : summary.costTrialCount > 0 &&
            hasDisplayableCostUsd(summary.costUsd) ? (
            <>
              {summary.costHasEstimated && !summary.costHasNative && (
                <span className="font-mono text-[16px] text-[color:var(--paper-ink-3)]">
                  ~
                </span>
              )}
              {formatCostUsd(summary.costUsd)}
              {summary.costHasEstimated && summary.costHasNative && (
                <span className="font-mono text-[16px] text-[color:var(--paper-ink-3)]">
                  *
                </span>
              )}
            </>
          ) : (
            <span className="text-[color:var(--paper-ink-3)]">—</span>
          )}
          {!costPending && (
            <QaCostSuffix
              costUsd={summary.qaCostUsd}
              size="tile"
              title={
                summary.qaHasEstimated
                  ? "QA/analysis spend across this experiment's trials. Some values estimated from token counts × static model pricing. Not included in the cost figure."
                  : "QA/analysis spend across this experiment's trials. Not included in the cost figure."
              }
            />
          )}
        </span>
        {!costPending && summary.tokenTrialCount > 0 && (
          <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
            {formatTokenCount(summary.tokenCount)}
          </span>
        )}
      </KpiTile>
      {showNewSpend && (
        <KpiTile
          label="New spend"
          labelInfo="Spend from trials this experiment ran itself — excludes trials gathered from other experiments."
        >
          <span
            className="font-display flex items-baseline gap-1 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]"
            title={
              costPending
                ? "Calculating new spend…"
                : summary.ownedTrialCount > 0
                  ? `Summed across ${summary.ownedTrialCount} trial${
                      summary.ownedTrialCount === 1 ? "" : "s"
                    } this experiment ran itself${
                      // Billing attribution is a property of who pays, not of
                      // what the experiment did; surface it here rather than
                      // in the headline.
                      summary.billedTrialCount > 0
                        ? `. ${formatCostUsd(summary.billedCostUsd)} of this was billed to user quotas`
                        : ". None of it was billed to a user quota"
                    }${
                      costIsSpend
                        ? ". The table shows only current-version trials"
                        : ""
                    }${
                      summary.ownedHasEstimated && summary.ownedHasNative
                        ? ". Mixed native + estimated values; ~ marks estimates."
                        : summary.ownedHasEstimated
                          ? ". Estimated from token counts × static model pricing."
                          : ". Reported by the agent runtime."
                    }`
                  : // Owned usage first: an experiment whose own trials
                    // reported tokens but no priced cost DID run work — it
                    // must not read as a pure collection.
                    summary.ownedTokenTrialCount > 0
                    ? "No cost data reported yet for this experiment's own trials"
                    : summary.costTrialCount > 0
                      ? "This experiment ran no trials of its own; every priced trial shown was gathered from another experiment, where its spend is reported."
                      : "No spend from this experiment yet"
            }
          >
            {costPending ? (
              <span className="text-[color:var(--paper-ink-3)]">—</span>
            ) : summary.ownedTrialCount > 0 ? (
              <>
                {summary.ownedHasEstimated && !summary.ownedHasNative && (
                  <span className="font-mono text-[16px] text-[color:var(--paper-ink-3)]">
                    ~
                  </span>
                )}
                {formatCostUsd(summary.ownedCostUsd)}
                {summary.ownedHasEstimated && summary.ownedHasNative && (
                  <span className="font-mono text-[16px] text-[color:var(--paper-ink-3)]">
                    *
                  </span>
                )}
              </>
            ) : summary.ownedTokenTrialCount === 0 && summary.costTrialCount > 0 ? (
              // Priced work exists and this experiment's own trials reported
              // nothing at all: an explicit zero ("nothing new was spent")
              // reads honestly where a dash would read as "unknown". With
              // owned usage awaiting pricing, the dash is the honest one.
              <>{formatCostUsd(0)}</>
            ) : (
              <span className="text-[color:var(--paper-ink-3)]">—</span>
            )}
            {!costPending && (
              <QaCostSuffix
                costUsd={summary.ownedQaCostUsd}
                size="tile"
                title="QA/analysis spend on this experiment's own trials. Not included in the new spend figure."
              />
            )}
          </span>
          {!costPending && summary.ownedTokenTrialCount > 0 && (
            <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
              {formatTokenCount(summary.ownedTokenCount)}
            </span>
          )}
        </KpiTile>
      )}
      <KpiTile
        label="Outcome distribution"
        className="col-span-2 md:col-span-1"
      >
        <div className="flex h-1.5 overflow-hidden rounded-[3px] bg-[color:var(--paper-bg-2)]">
          <span
            style={{ width: `${passPct}%`, background: "var(--paper-pass)" }}
          />
          <span
            style={{
              width: `${partialPct}%`,
              background: "var(--paper-partial)",
            }}
          />
          <span
            style={{ width: `${failPct}%`, background: "var(--paper-fail)" }}
          />
          <span
            style={{ width: `${errPct}%`, background: "var(--paper-error)" }}
          />
          <span
            style={{
              width: `${skippedPct}%`,
              background: "var(--paper-ink-3)",
            }}
          />
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-[color:var(--paper-ink-2)]">
          <span className="inline-flex items-center gap-1.5">
            <i className="inline-block h-2 w-2 rounded-[2px] bg-[color:var(--paper-pass)]" />
            {summary.passCount}
            <span className="text-[color:var(--paper-ink-3)]">pass</span>
          </span>
          {summary.partialCount > 0 && (
            <span className="inline-flex items-center gap-1.5">
              <i className="inline-block h-2 w-2 rounded-[2px] bg-[color:var(--paper-partial)]" />
              {summary.partialCount}
              <span className="text-[color:var(--paper-ink-3)]">partial</span>
            </span>
          )}
          <span className="inline-flex items-center gap-1.5">
            <i className="inline-block h-2 w-2 rounded-[2px] bg-[color:var(--paper-fail)]" />
            {summary.failCount}
            <span className="text-[color:var(--paper-ink-3)]">fail</span>
          </span>
          {summary.harnessErrorCount > 0 && (
            <span className="inline-flex items-center gap-1.5">
              <i className="inline-block h-2 w-2 rounded-[2px] bg-[color:var(--paper-error)]" />
              {summary.harnessErrorCount}
              <span className="text-[color:var(--paper-ink-3)]">error</span>
            </span>
          )}
          {summary.skippedTrials > 0 && (
            <span className="inline-flex items-center gap-1.5">
              <i className="inline-block h-2 w-2 rounded-[2px] bg-[color:var(--paper-ink-3)]" />
              {summary.skippedTrials}
              <span className="text-[color:var(--paper-ink-3)]">skipped</span>
            </span>
          )}
        </div>
      </KpiTile>
    </div>
  );
}

export function ExperimentDetailView({
  experimentId,
  tasksForExperiment,
  costTotals,
  costTotalsPending = false,
  isLoading,
  isLoadingTrials = false,
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
  // Task-definition pane addressing. The drawer can show the task's file
  // tree beside the trial view, so the two panes address independently:
  // the trial pane owns ?file= / ?lines= (see TrialDetailPanel) and the
  // task pane owns ?taskFile= / ?taskLines=.
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
    }
    lastDrawerTaskIdRef.current = taskId;
  }, [drawerState?.task.id, handleTaskPaneFileChange]);
  // Probe cells open main's sliding ProbeDetailPanel (kept from origin/main).
  // On the slim experiment path the grid has no probe trials to click, so this
  // stays dormant until probes are fed to that path -- the code is retained so
  // main's probe-drawer feature is preserved and the merge stays coherent.
  const [probeDrawer, setProbeDrawer] = useState<{
    taskId: string;
    trialId: string;
  } | null>(null);
  const [showPassAtK, setShowPassAtK] = useState(readOnly);
  const [showTask, setShowTask] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    try {
      const stored = window.localStorage.getItem(
        "oddish:trial-drawer-show-task"
      );
      // Default ON: only explicit "0" disables it.
      return stored !== "0";
    } catch {
      return true;
    }
  });
  const [showTrial, setShowTrial] = useState<boolean>(() => {
    if (typeof window === "undefined") return true;
    try {
      const stored = window.localStorage.getItem(
        "oddish:trial-drawer-show-trial"
      );
      return stored !== "0";
    } catch {
      return true;
    }
  });

  const handleShowTaskChange = useCallback((next: boolean) => {
    setShowTask(next);
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        "oddish:trial-drawer-show-task",
        next ? "1" : "0"
      );
    } catch {
      // ignore
    }
  }, []);

  const handleShowTrialChange = useCallback((next: boolean) => {
    setShowTrial(next);
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        "oddish:trial-drawer-show-trial",
        next ? "1" : "0"
      );
    } catch {
      // ignore
    }
  }, []);
  const [cachedAgentSummaries, setCachedAgentSummaries] = useState<
    ExperimentAgentSummary[]
  >([]);
  const hydratedFromUrl = useRef(false);
  // Deep-linked ?trial= that wasn't in the loaded data when the URL was read:
  // trial pages stream in after the task shells, so on direct loads the trial
  // is almost never there yet. Held here until it resolves from a streamed
  // page or a direct /api/trials fetch.
  const [pendingUrlTrialId, setPendingUrlTrialId] = useState<string | null>(
    null
  );
  // A pending deep-link trial fetched directly by id, staged until its host
  // task shell is available to open the drawer with.
  const [resolvedUrlTrial, setResolvedUrlTrial] = useState<Trial | null>(null);
  // Task drawer opened by hydration itself while a deep-link trial was still
  // pending (possibly from a stale ?task= naming the wrong task). The
  // resolver may replace this drawer; any other open drawer means the user
  // navigated, and the deep link yields.
  const hydrationTaskIdRef = useRef<string | null>(null);
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
        next.set(
          "trial",
          shortTrialParam(drawerState.trial.id, drawerState.trial.task_id),
        );
      } else if (pendingUrlTrialId == null) {
        // While a deep-linked trial is still resolving, the drawer is in task
        // mode but the ?trial= param must survive for the promotion to keep
        // the URL truthful.
        next.delete("trial");
        next.delete("tab");
        next.delete("file");
        next.delete("lines");
      }
      if (taskPaneFile) {
        next.set("taskFile", taskPaneFile);
      } else {
        next.delete("taskFile");
      }
      if (taskPaneLines) {
        next.set("taskLines", formatLineRange(taskPaneLines));
      } else {
        next.delete("taskLines");
      }
    } else if (pendingUrlTrialId == null) {
      // Same pending guard as above: a trial-only deep link keeps the drawer
      // closed until the trial resolves, and stripping the params here would
      // destroy the address it's resolving from.
      next.delete("task");
      next.delete("trial");
      next.delete("tab");
      next.delete("file");
      next.delete("lines");
      next.delete("taskFile");
      next.delete("taskLines");
    }

    if (next.toString() !== current.toString()) {
      const url = `${window.location.pathname}${next.toString() ? `?${next.toString()}` : ""}`;
      // Keep URL query in sync without triggering app-router navigation work.
      window.history.replaceState(window.history.state, "", url);
    }
  }, [drawerState, pendingUrlTrialId, taskPaneFile, taskPaneLines]);

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

    // ?trial= is an index against the task in the address; older links spell
    // the whole id out and pass through untouched.
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
            trial,
            trialIndex: orderedTrials.findIndex((t) => t.id === trial.id),
            orderedTrials,
            trialGroups,
          });
          return;
        }
      }
      // Not loaded yet: keep the id pending; it resolves from a streamed
      // trial page or the direct /api/trials fetch. Remember which task
      // drawer hydration opens below so the resolver may replace it — it
      // must never replace one the user opened themselves.
      setPendingUrlTrialId(urlTrialId);
      hydrationTaskIdRef.current = task?.id ?? null;
    }

    if (!task) return;

    const taskIndex = tasksForExperiment.indexOf(task);
    const { trialGroups, orderedTrials } = buildTrialGroups(task);
    setDrawerState({
      isOpen: true,
      mode: "task",
      task,
      taskIndex,
      orderedTasks: tasksForExperiment,
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
    const liveTrialCount = liveTask.trials?.length ?? 0;
    const snapshotTrialCount = drawerState.task.trials?.length ?? 0;
    if (
      liveTask === drawerState.task &&
      liveTrialCount === snapshotTrialCount
    ) {
      return;
    }
    const { trialGroups, orderedTrials } = buildTrialGroups(liveTask);
    const foundTrialIndex = drawerState.trial
      ? orderedTrials.findIndex((t) => t.id === drawerState.trial!.id)
      : -1;
    const resolvedTrialIndex = foundTrialIndex >= 0 ? foundTrialIndex : null;
    const resolvedTrial =
      resolvedTrialIndex != null
        ? orderedTrials[resolvedTrialIndex]
        : drawerState.trial;
    const resolvedTaskIndex = tasksForExperiment.indexOf(liveTask);
    setDrawerState({
      ...drawerState,
      task: liveTask,
      taskIndex:
        resolvedTaskIndex >= 0 ? resolvedTaskIndex : drawerState.taskIndex,
      orderedTasks: tasksForExperiment,
      trial: resolvedTrial,
      trialIndex: resolvedTrialIndex,
      orderedTrials,
      trialGroups,
    });
  }, [tasksForExperiment, drawerState, buildTrialGroups]);

  // Any drawer change the user makes themselves cancels an unresolved deep
  // link: a late resolve must never yank them away from where they went.
  const cancelPendingDeepLink = useCallback(() => {
    setPendingUrlTrialId(null);
    setResolvedUrlTrial(null);
    hydrationTaskIdRef.current = null;
  }, []);

  // Open a resolved deep-link trial. Yields if the user has navigated on
  // their own since hydration: only a closed drawer, the host task's own
  // task-mode drawer, or the task drawer hydration itself opened (possibly
  // off a stale ?task=) may be replaced. Yielding still clears the pending
  // state — a deep link never overrides the user.
  const openDeepLinkTrial = useCallback(
    (host: Task, trial: Trial) => {
      setDrawerState((prev) => {
        if (
          prev &&
          !(
            prev.mode === "task" &&
            (prev.task.id === host.id ||
              prev.task.id === hydrationTaskIdRef.current)
          )
        ) {
          return prev;
        }
        const { trialGroups, orderedTrials } = buildTrialGroups(host);
        const index = orderedTrials.findIndex((t) => t.id === trial.id);
        return {
          isOpen: true,
          mode: "trial",
          task: host,
          taskIndex: tasksForExperiment.indexOf(host),
          orderedTasks: tasksForExperiment,
          trial: index >= 0 ? orderedTrials[index] : trial,
          trialIndex: index >= 0 ? index : null,
          orderedTrials,
          trialGroups,
        };
      });
      setPendingUrlTrialId(null);
      setResolvedUrlTrial(null);
    },
    [tasksForExperiment, buildTrialGroups]
  );

  // Resolve a pending deep-link trial from grid data as it streams in. This
  // is the only resolution path public share pages have (they can't use the
  // authed by-id route), and it also covers the authed page whenever the
  // direct fetch below is slow or failed transiently.
  useEffect(() => {
    if (pendingUrlTrialId == null) return;
    for (const host of tasksForExperiment) {
      const trial = (host.trials ?? []).find(
        (t) => t.id === pendingUrlTrialId
      );
      if (trial) {
        openDeepLinkTrial(host, trial);
        return;
      }
    }
    // Public share pages have no by-id fetch, so this scan is their only
    // resolution source: once everything the page will ever have is loaded
    // and the id still isn't there, the deep link is dead — give it up so
    // URL sync can drop the stale param.
    if (!loadFullTrialOnOpen && !isLoading && !isLoadingTrials) {
      setPendingUrlTrialId(null);
    }
  }, [
    pendingUrlTrialId,
    tasksForExperiment,
    openDeepLinkTrial,
    loadFullTrialOnOpen,
    isLoading,
    isLoadingTrials,
  ]);

  // A deep-linked trial can also point at data the grid will never stream in
  // (a task beyond the prefetched pages, or a superseded trial), so resolve
  // the pending id with a direct fetch too. The fetched trial is only staged
  // here; the effect below opens it once its host task shell is known.
  // Whichever source lands first wins: a resolve from the streamed path
  // clears the pending id, which cancels this fetch. Transient failures
  // retry with backoff (the streamed path keeps running meanwhile); a
  // definitive 404 or exhausted retries give the deep link up so the
  // pending state and stale URL params don't outlive their chances.
  useEffect(() => {
    if (pendingUrlTrialId == null || !loadFullTrialOnOpen) return;
    let cancelled = false;
    (async () => {
      const MAX_ATTEMPTS = 3;
      for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
        try {
          const res = await fetch(
            `${apiBaseUrl}/trials/${encodeURIComponent(pendingUrlTrialId)}`,
            { cache: "no-store" }
          );
          if (cancelled) return;
          if (res.ok) {
            const fetched = (await res.json()) as Trial;
            if (!cancelled) setResolvedUrlTrial(fetched);
            return;
          }
          if (res.status === 404) break;
        } catch {
          // Transient network failure — retry below.
        }
        if (cancelled) return;
        if (attempt < MAX_ATTEMPTS) {
          await new Promise((resolve) =>
            setTimeout(resolve, 1000 * 2 ** (attempt - 1))
          );
          if (cancelled) return;
        }
      }
      if (!cancelled) setPendingUrlTrialId(null);
    })();
    return () => {
      cancelled = true;
    };
  }, [pendingUrlTrialId, loadFullTrialOnOpen, apiBaseUrl]);

  // Open a directly-fetched deep-link trial. The trial is the source of
  // truth: its task_id names the host task, so the link works even when the
  // ?task= param is missing or names the wrong task. Waits for the host
  // task's shell to arrive if it hasn't yet (shells cover the whole
  // experiment in one request).
  useEffect(() => {
    if (!resolvedUrlTrial) return;
    // A cancelled deep link stays cancelled: an in-flight fetch can write
    // resolvedUrlTrial back after cancelPendingDeepLink cleared it (both
    // updates land in the same batch, last write wins). The pending id is
    // nulled by every cancel, so a mismatch means this value is a late
    // revival — drop it instead of reopening a drawer the user closed.
    if (pendingUrlTrialId !== resolvedUrlTrial.id) {
      setResolvedUrlTrial(null);
      return;
    }
    const host = tasksForExperiment.find(
      (t) => t.id === resolvedUrlTrial.task_id
    );
    if (!host) {
      // Shells cover the whole experiment in one request, but slim-task
      // pages can still merge in hosts the shells never returned (shells
      // are capped, and the trials merge appends enriched-only tasks). So
      // the deep link only gives up once BOTH loads are done and the host
      // still isn't there — then it truly belongs to another experiment
      // or an unlinked task, and URL sync may drop the stale params.
      if (!isLoading && !isLoadingTrials && tasksForExperiment.length > 0) {
        cancelPendingDeepLink();
      }
      return;
    }
    openDeepLinkTrial(host, resolvedUrlTrial);
  }, [
    resolvedUrlTrial,
    pendingUrlTrialId,
    tasksForExperiment,
    openDeepLinkTrial,
    isLoading,
    isLoadingTrials,
    cancelPendingDeepLink,
  ]);

  // Prefer the server-side rollup for cost: ``buildExperimentSummary`` sums
  // only the loaded pages, and only the trials the grid renders, so it
  // understates spend on both counts. Non-cost fields stay client-side --
  // they describe the visible rows, which is what they should describe.
  const summary = useMemo(() => {
    const base = buildExperimentSummary(deferredTasksForDerivedData);
    if (!costTotals) return base;
    return {
      ...base,
      costUsd: costTotals.cost_usd,
      costTrialCount: costTotals.cost_trial_count,
      costHasEstimated: costTotals.cost_has_estimated,
      costHasNative: costTotals.cost_has_native,
      qaCostUsd: costTotals.qa_cost_usd ?? 0,
      ownedQaCostUsd: costTotals.owned_qa_cost_usd ?? 0,
      qaHasEstimated: costTotals.qa_has_estimated ?? false,
      tokenCount: costTotals.token_count,
      tokenTrialCount: costTotals.token_trial_count,
      // ?? base.*: deploy-skew guard — a backend that predates owned_* omits
      // the fields; the client fold's partial owned sum beats a hard $0.00.
      ownedCostUsd: costTotals.owned_cost_usd ?? base.ownedCostUsd,
      ownedTrialCount: costTotals.owned_trial_count ?? base.ownedTrialCount,
      ownedHasEstimated: costTotals.owned_has_estimated ?? base.ownedHasEstimated,
      ownedHasNative: costTotals.owned_has_native ?? base.ownedHasNative,
      ownedTokenCount: costTotals.owned_token_count ?? base.ownedTokenCount,
      ownedTokenTrialCount:
        costTotals.owned_token_trial_count ?? base.ownedTokenTrialCount,
      billedCostUsd: costTotals.billed_cost_usd,
      billedTrialCount: costTotals.billed_trial_count,
      billedHasEstimated: costTotals.billed_has_estimated,
      billedHasNative: costTotals.billed_has_native,
      billedTokenCount: costTotals.billed_token_count,
      billedTokenTrialCount: costTotals.billed_token_trial_count,
    };
  }, [deferredTasksForDerivedData, costTotals]);

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

  const handleNavigateToTrial = (trial: Trial, trialIndex: number) => {
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
        <div className="flex min-h-[240px] items-center justify-center rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] py-10">
          <div className="inline-flex items-center gap-2 rounded-md border border-[color:var(--paper-line)] bg-[color:var(--paper-surface-2)] px-3 py-2 text-sm text-[color:var(--paper-ink-3)]">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span>Loading experiment...</span>
          </div>
        </div>
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
            taskCount={tasksForExperiment.length}
            summary={summary}
            isInitialLoading={isInitialLoading}
            isLoadingTrials={isLoadingTrials}
            // The owned-vs-gathered spend split (and the billing attribution
            // in its tooltip) is internal; keep it off the public share view
            // (the only readOnly consumer).
            showNewSpend={!readOnly}
            costIsSpend={costTotals != null}
            costPending={costTotalsPending}
          />

          {hasError ? (
            <Alert variant="destructive">
              <AlertTitle>{errorTitle}</AlertTitle>
              <AlertDescription>{errorDescription}</AlertDescription>
            </Alert>
          ) : (
            <div className="space-y-3">
              {inlineAlert}
              <ExperimentTrialsTable
                tasks={tasksForExperiment}
                agentSummaries={displayAgentSummaries}
                modelScopedAgents={displayModelScopedAgents}
                isLoading={isLoading}
                isLoadingTrials={isLoadingTrials}
                showPassAtK={showPassAtK}
                onTaskUnlink={onTaskUnlink}
                onRerun={onRerun}
                allowRerun={allowRetry}
                readOnly={readOnly}
                showAnalysis={showAnalysis}
                onTrialSelect={(trial, task, context) => {
                  cancelPendingDeepLink();
                  const taskIndex = tasksForExperiment.findIndex(
                    (t) => t.id === task.id
                  );
                  setDrawerState({
                    isOpen: true,
                    mode: "trial",
                    task,
                    taskIndex: taskIndex >= 0 ? taskIndex : 0,
                    orderedTasks: tasksForExperiment,
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
                    trial: null,
                    trialIndex: null,
                    orderedTrials,
                    trialGroups,
                  });
                }}
              />
            </div>
          )}
        </div>
      )}

      {drawerState && (
        <UnifiedDrawerWrapper
          open={drawerState.isOpen}
          onOpenChange={(open) => !open && closeDrawer()}
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
              showAnalysis={showAnalysis}
              contentOnly={true}
            />
          }
          taskContent={
            <TaskFilesPanel
              isOpen={drawerState.mode === "task"}
              onClose={closeDrawer}
              taskId={drawerState.task.id}
              task={drawerState.task}
              taskVersion={resolveExperimentTaskVersion(drawerState.task)}
              orderedTasks={drawerState.orderedTasks}
              taskIndex={drawerState.taskIndex}
              onRetryComplete={onRerun}
              allowRetry={allowRetry}
              showAnalysis={showAnalysis}
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
                revalidateTrial={loadFullTrialOnOpen}
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
