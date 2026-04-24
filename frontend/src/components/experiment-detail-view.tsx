"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { ExperimentTrialsTable } from "@/components/experiment-trials-table";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { UnifiedDrawerWrapper } from "@/components/unified-drawer-wrapper";
import type { Task, Trial } from "@/lib/types";
import { Loader2 } from "lucide-react";
import {
  buildExperimentAgentSummaries,
  getExperimentAgentKey,
  type ExperimentAgentSummary,
} from "@/lib/experiment-agent-grouping";

type DrawerMode = "task" | "trial";

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
  isLoading: boolean;
  isLoadingTrials?: boolean;
  hasError?: boolean;
  errorTitle?: string;
  errorDescription?: string;
  headerLeft: React.ReactNode;
  headerStatus?: React.ReactNode;
  headerRight?: React.ReactNode;
  inlineAlert?: React.ReactNode;
  readOnly?: boolean;
  allowRetry?: boolean;
  apiBaseUrl?: string;
  onTaskDelete?: (task: Task) => Promise<void>;
  onTrialDelete?: (trial: Trial, task: Task | null) => Promise<void>;
  onRerun?: (taskIds?: string[]) => void;
}

const AGENT_SUMMARY_STORAGE_PREFIX = "oddish:experiment-agent-summaries:";

function getModelScopedAgentsFromSummaries(
  summaries: ExperimentAgentSummary[],
): Set<string> {
  return new Set(
    summaries
      .filter((summary) => summary.isModelScoped)
      .map((summary) => summary.agent),
  );
}

type ExperimentSummary = {
  rewardSuccess: number;
  rewardSum: number;
  rewardTotal: number;
  totalTrials: number;
  completedTrials: number;
  failedTrials: number;
  passCount: number;
  partialCount: number;
  failCount: number;
  harnessErrorCount: number;
  pendingCount: number;
  costUsd: number;
  costTrialCount: number;
  costHasEstimated: boolean;
  costHasNative: boolean;
};

function buildExperimentSummary(tasksForExperiment: Task[]): ExperimentSummary {
  let rewardSuccess = 0;
  let rewardSum = 0;
  let rewardTotal = 0;
  let totalTrials = 0;
  let completedTrials = 0;
  let failedTrials = 0;

  let passCount = 0;
  let partialCount = 0;
  let failCount = 0;
  let harnessErrorCount = 0;
  let pendingCount = 0;

  let costUsd = 0;
  let costTrialCount = 0;
  let costHasEstimated = false;
  let costHasNative = false;

  for (const task of tasksForExperiment) {
    const trials = task.trials ?? [];
    if (trials.length > 0) {
      // Compute from the (already version-filtered) trials array
      for (const trial of trials) {
        if (trial.cost_usd != null) {
          costUsd += trial.cost_usd;
          costTrialCount += 1;
          if (trial.cost_is_estimated === true) {
            costHasEstimated = true;
          } else {
            costHasNative = true;
          }
        }
        if (trial.status === "success" && trial.reward != null) {
          rewardSum += trial.reward;
          rewardTotal++;
          if (trial.reward === 1) {
            passCount++;
            rewardSuccess++;
          } else if (trial.reward === 0) {
            failCount++;
          } else {
            partialCount++;
          }
        } else if (trial.status === "success" && trial.reward == null) {
          // Completed but reward not yet set
        } else if (trial.status === "failed") {
          harnessErrorCount++;
        } else {
          pendingCount++;
        }
      }
      totalTrials += trials.length;
      completedTrials += trials.filter((t) => t.status === "success").length;
      failedTrials += trials.filter((t) => t.status === "failed").length;
    } else {
      // Trials not loaded yet — fall back to server-provided aggregates
      rewardSuccess += task.reward_success ?? 0;
      rewardSum += task.reward_sum ?? task.reward_success ?? 0;
      rewardTotal += task.reward_total ?? 0;
      totalTrials += task.total;
      completedTrials += task.completed;
      failedTrials += task.failed;
    }
  }

  return {
    rewardSuccess,
    rewardSum,
    rewardTotal,
    totalTrials,
    completedTrials,
    failedTrials,
    passCount,
    partialCount,
    failCount,
    harnessErrorCount,
    pendingCount,
    costUsd,
    costTrialCount,
    costHasEstimated,
    costHasNative,
  };
}

function formatCostUsd(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 1) return `$${value.toFixed(3)}`;
  if (value < 100) return `$${value.toFixed(2)}`;
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function ExperimentHeaderMeta({
  isLoading,
  isInitialLoading,
  headerStatus,
  showPassAtK,
  onToggleShowPassAtK,
  headerRight,
}: {
  isLoading: boolean;
  isInitialLoading: boolean;
  headerStatus?: React.ReactNode;
  showPassAtK: boolean;
  onToggleShowPassAtK: () => void;
  headerRight?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {isLoading && (
        <div className="inline-flex items-center gap-1.5 rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface-2)] px-2 py-1 text-xs text-[color:var(--paper-ink-3)]">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          <span>{isInitialLoading ? "Loading tasks..." : "Refreshing..."}</span>
        </div>
      )}
      {headerStatus}
      <button
        type="button"
        onClick={onToggleShowPassAtK}
        aria-pressed={showPassAtK}
        className={`inline-flex h-8 select-none items-center gap-[7px] rounded-[7px] border px-3 text-[12px] font-medium leading-none transition-colors ${
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
        Pass@k graph
      </button>
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
  let earliest: Task = tasks[0];
  for (const task of tasks) {
    if (
      new Date(task.created_at).getTime() <
      new Date(earliest.created_at).getTime()
    ) {
      earliest = task;
    }
  }
  return {
    createdAt: earliest.created_at,
    author: earliest.github_username || earliest.user || null,
  };
}

function ExperimentMetaStrip({
  tasks,
  isInitialLoading,
  experimentId,
}: {
  tasks: Task[];
  isInitialLoading: boolean;
  experimentId?: string;
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
  if (!createdAt && !author && !experimentId) return null;

  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 font-mono text-[11.5px] text-[color:var(--paper-ink-3)]">
      {createdAt && (
        <span title={new Date(createdAt).toLocaleString()}>
          created {formatRelativeTime(createdAt)}
        </span>
      )}
      {createdAt && author && <MetaDot />}
      {author && <span>by {author}</span>}
      {(createdAt || author) && experimentId && <MetaDot />}
      {experimentId && (
        <span className="inline-flex items-center gap-1">
          <span>id</span>
          <button
            type="button"
            onClick={handleCopyExperimentId}
            className="cursor-pointer rounded-sm text-[color:var(--paper-ink-2)] transition hover:text-[color:var(--paper-ink)]"
            aria-label={`Copy experiment id ${experimentId}`}
            title={copied ? "Copied" : "Click to copy experiment id"}
          >
            <span className="select-all">{experimentId}</span>
          </button>
          {copied && <span aria-live="polite">copied</span>}
        </span>
      )}
    </div>
  );
}

function KpiTile({
  label,
  children,
  className = "",
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex flex-col gap-1.5 border-r border-[color:var(--paper-line-2)] px-4 py-3 last:border-r-0 ${className}`}
    >
      <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.09em] text-[color:var(--paper-ink-3)]">
        {label}
      </span>
      {children}
    </div>
  );
}

function ExperimentSummaryBar({
  taskCount,
  summary,
  isInitialLoading,
}: {
  taskCount: number;
  summary: ExperimentSummary;
  isInitialLoading: boolean;
}) {
  if (isInitialLoading) {
    return (
      <div className="flex items-center gap-2 rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-3 text-xs text-[color:var(--paper-ink-3)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Loading experiment summary...
      </div>
    );
  }

  const scorePct =
    summary.rewardTotal > 0
      ? (summary.rewardSum / summary.rewardTotal) * 100
      : null;
  const completionPct =
    summary.totalTrials > 0
      ? (summary.completedTrials / summary.totalTrials) * 100
      : 0;
  const outcomeTotal =
    summary.passCount +
    summary.partialCount +
    summary.failCount +
    summary.harnessErrorCount;
  const passPct = outcomeTotal ? (summary.passCount / outcomeTotal) * 100 : 0;
  const partialPct = outcomeTotal
    ? (summary.partialCount / outcomeTotal) * 100
    : 0;
  const failPct = outcomeTotal ? (summary.failCount / outcomeTotal) * 100 : 0;
  const errPct = outcomeTotal
    ? (summary.harnessErrorCount / outcomeTotal) * 100
    : 0;

  return (
    <div className="grid grid-cols-2 overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] md:grid-cols-[1.1fr_1fr_0.9fr_0.9fr_1.4fr]">
      <KpiTile label="Avg score">
        <span className="flex items-baseline gap-2 font-display text-[26px] font-medium leading-none tracking-[-0.02em] text-[color:var(--paper-ink)]">
          {scorePct != null ? `${scorePct.toFixed(1)}%` : "—"}
        </span>
      </KpiTile>
      <KpiTile label="Completion">
        <span className="flex items-baseline gap-2 font-display text-[26px] font-medium leading-none tracking-[-0.02em] text-[color:var(--paper-ink)]">
          {summary.completedTrials}
          <span className="font-mono text-xs font-normal text-[color:var(--paper-ink-3)]">
            / {summary.totalTrials} trials
          </span>
        </span>
        <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
          {completionPct.toFixed(0)}%
          {summary.failedTrials > 0 && (
            <span className="ml-1.5 text-[color:var(--paper-fail)]">
              · {summary.failedTrials} failing
            </span>
          )}
        </span>
      </KpiTile>
      <KpiTile label="Tasks">
        <span className="flex items-baseline gap-2 font-display text-[26px] font-medium leading-none tracking-[-0.02em] text-[color:var(--paper-ink)]">
          {taskCount}
          <span className="font-mono text-xs font-normal text-[color:var(--paper-ink-3)]">
            tasks
          </span>
        </span>
      </KpiTile>
      <KpiTile label="Cost">
        <span
          className="flex items-baseline gap-1 font-display text-[26px] font-medium leading-none tracking-[-0.02em] text-[color:var(--paper-ink)]"
          title={
            summary.costTrialCount > 0
              ? `Summed across ${summary.costTrialCount} trial${
                  summary.costTrialCount === 1 ? "" : "s"
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
          {summary.costTrialCount > 0 ? (
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
        </span>
      </KpiTile>
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
        </div>
      </KpiTile>
    </div>
  );
}

export function ExperimentDetailView({
  experimentId,
  tasksForExperiment,
  isLoading,
  isLoadingTrials = false,
  hasError = false,
  errorTitle = "Failed to load experiment",
  errorDescription = "Check the API connection and try again.",
  headerLeft,
  headerStatus,
  headerRight,
  inlineAlert,
  readOnly = false,
  allowRetry = true,
  apiBaseUrl = "/api",
  onTaskDelete,
  onTrialDelete,
  onRerun,
}: ExperimentDetailViewProps) {
  const searchParams = useSearchParams();
  const [drawerState, setDrawerState] = useState<DrawerState>(null);
  const [showPassAtK, setShowPassAtK] = useState(false);
  const [cachedAgentSummaries, setCachedAgentSummaries] = useState<
    ExperimentAgentSummary[]
  >([]);
  const hydratedFromUrl = useRef(false);
  const isInitialLoading = isLoading && tasksForExperiment.length === 0;

  const agentSummaryStorageKey = experimentId
    ? `${AGENT_SUMMARY_STORAGE_PREFIX}${experimentId}`
    : null;
  const { agentSummaries, modelScopedAgents } = useMemo(
    () => buildExperimentAgentSummaries(tasksForExperiment),
    [tasksForExperiment],
  );
  const displayAgentSummaries =
    agentSummaries.length > 0 ? agentSummaries : cachedAgentSummaries;
  const displayModelScopedAgents = useMemo(
    () =>
      agentSummaries.length > 0
        ? modelScopedAgents
        : getModelScopedAgentsFromSummaries(cachedAgentSummaries),
    [agentSummaries, modelScopedAgents, cachedAgentSummaries],
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
        JSON.stringify(agentSummaries),
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
    [displayModelScopedAgents],
  );

  useEffect(() => {
    if (!hydratedFromUrl.current) return;

    const next = new URLSearchParams(searchParams.toString());
    if (drawerState?.isOpen) {
      next.set("task", drawerState.task.id);
      if (drawerState.mode === "trial" && drawerState.trial) {
        next.set("trial", drawerState.trial.id);
      } else {
        next.delete("trial");
        next.delete("tab");
        next.delete("file");
      }
    } else {
      next.delete("task");
      next.delete("trial");
      next.delete("tab");
      next.delete("file");
    }

    if (next.toString() !== searchParams.toString()) {
      const url = `${window.location.pathname}${next.toString() ? `?${next.toString()}` : ""}`;
      // Keep URL query in sync without triggering app-router navigation work.
      window.history.replaceState(window.history.state, "", url);
    }
  }, [drawerState, searchParams]);

  useEffect(() => {
    if (hydratedFromUrl.current || tasksForExperiment.length === 0) return;
    hydratedFromUrl.current = true;

    const urlTaskId = searchParams.get("task");
    const urlTrialId = searchParams.get("trial");
    if (!urlTaskId) return;

    const task = tasksForExperiment.find((t) => t.id === urlTaskId);
    if (!task) return;

    const taskIndex = tasksForExperiment.indexOf(task);
    const { trialGroups, orderedTrials } = buildTrialGroups(task);

    if (urlTrialId) {
      const trial = orderedTrials.find((t) => t.id === urlTrialId) ?? null;
      if (trial) {
        const trialIndex = orderedTrials.indexOf(trial);
        setDrawerState({
          isOpen: true,
          mode: "trial",
          task,
          taskIndex,
          orderedTasks: tasksForExperiment,
          trial,
          trialIndex,
          orderedTrials,
          trialGroups,
        });
        return;
      }
    }

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
      (t) => t.id === drawerState.task.id,
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

  const summary = useMemo(
    () => buildExperimentSummary(tasksForExperiment),
    [tasksForExperiment],
  );

  const closeDrawer = () => {
    setDrawerState(null);
  };

  const handleNavigateToFirstTrial = () => {
    if (!drawerState) return;
    const firstGroup = drawerState.trialGroups[0];
    if (!firstGroup || firstGroup.trials.length === 0) return;

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
    setDrawerState({
      ...drawerState,
      mode: "task",
      trial: null,
      trialIndex: null,
    });
  };

  const handleNavigateToTrial = (trial: Trial, trialIndex: number) => {
    if (!drawerState) return;
    setDrawerState({
      ...drawerState,
      mode: "trial",
      trial,
      trialIndex,
    });
  };

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
          <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <div className="min-w-0">{headerLeft}</div>
              <ExperimentMetaStrip
                tasks={tasksForExperiment}
                isInitialLoading={isInitialLoading}
                experimentId={experimentId}
              />
            </div>
            <ExperimentHeaderMeta
              isLoading={isLoading}
              isInitialLoading={isInitialLoading}
              headerStatus={headerStatus}
              showPassAtK={showPassAtK}
              onToggleShowPassAtK={() => setShowPassAtK((prev) => !prev)}
              headerRight={headerRight}
            />
          </div>

          <ExperimentSummaryBar
            taskCount={tasksForExperiment.length}
            summary={summary}
            isInitialLoading={isInitialLoading}
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
                onTaskDelete={onTaskDelete}
                onRerun={onRerun}
                allowRerun={allowRetry}
                readOnly={readOnly}
                onTrialSelect={(trial, task, context) => {
                  const taskIndex = tasksForExperiment.findIndex(
                    (t) => t.id === task.id,
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
                onTaskSelect={(task, context) => {
                  const { trialGroups, orderedTrials } = buildTrialGroups(task);
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
          taskContent={
            <TaskFilesPanel
              isOpen={true}
              onClose={closeDrawer}
              taskId={drawerState.task.id}
              task={drawerState.task}
              orderedTasks={drawerState.orderedTasks}
              taskIndex={drawerState.taskIndex}
              onRetryComplete={onRerun}
              allowRetry={allowRetry}
              onNavigate={(nextTask, nextIndex) => {
                if (!drawerState) return;
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
              apiBaseUrl={apiBaseUrl}
              contentOnly={true}
            />
          }
          trialContent={
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
                allowDelete={Boolean(onTrialDelete)}
                apiBaseUrl={apiBaseUrl}
                contentOnly={true}
              />
            )
          }
        />
      )}
    </>
  );
}
