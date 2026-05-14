"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useState } from "react";
import useSWR from "swr";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { TaskFilesPanel } from "@/components/task-files-panel";
import { TrialDetailPanel } from "@/components/trial-detail-panel";
import { UnifiedDrawerWrapper } from "@/components/unified-drawer-wrapper";
import { fetcher } from "@/lib/api";
import {
  buildExperimentAgentSummaries,
  getExperimentAgentKey,
} from "@/lib/experiment-agent-grouping";
import {
  formatPartialRewardBadgeValue,
  formatRewardPercent,
  formatRewardValue,
  getMatrixStatus,
  getRewardStyle,
  STATUS_CONFIG,
} from "@/lib/status-config";
import type {
  Task,
  TaskDetailResponse,
  TaskVersionSummary,
  Trial,
} from "@/lib/types";
import { formatRelativeTime } from "@/lib/utils";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  FileText,
  Loader2,
  Microscope,
  XCircle,
} from "lucide-react";

const ALL_VERSIONS_ID = "__all__";

function formatCostUsd(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 1) return `$${value.toFixed(3)}`;
  if (value < 100) return `$${value.toFixed(2)}`;
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function formatDurationSec(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds - m * 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m - h * 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

function trialDurationSec(trial: Trial): number | null {
  if (!trial.started_at || !trial.finished_at) return null;
  const start = new Date(trial.started_at).getTime();
  const end = new Date(trial.finished_at).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return (end - start) / 1000;
}

function CostBadge({
  cost,
  trialCount,
  hasEstimated,
  hasNative,
  size = "md",
}: {
  cost: number;
  trialCount: number;
  hasEstimated: boolean;
  hasNative: boolean;
  size?: "sm" | "md" | "lg";
}) {
  const valueClass =
    size === "lg"
      ? "text-[26px]"
      : size === "md"
        ? "text-[20px]"
        : "text-[13px]";
  const prefixClass =
    size === "lg" ? "text-[16px]" : size === "md" ? "text-[13px]" : "text-[10px]";
  const titleText =
    trialCount === 0
      ? "No cost data reported yet"
      : `Summed across ${trialCount} trial${trialCount === 1 ? "" : "s"}${
          hasEstimated && hasNative
            ? ". Mixed native + estimated values; ~ marks estimates."
            : hasEstimated
              ? ". Estimated from token counts × static model pricing."
              : ". Reported by the agent runtime."
        }`;

  if (trialCount === 0) {
    return (
      <span
        className={`font-display ${valueClass} leading-none tracking-[-0.02em] text-[color:var(--paper-ink-3)]`}
        title={titleText}
      >
        —
      </span>
    );
  }

  return (
    <span
      className={`font-display flex items-baseline gap-1 ${valueClass} leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]`}
      title={titleText}
    >
      {hasEstimated && !hasNative && (
        <span className={`font-mono ${prefixClass} text-[color:var(--paper-ink-3)]`}>
          ~
        </span>
      )}
      {formatCostUsd(cost)}
      {hasEstimated && hasNative && (
        <span className={`font-mono ${prefixClass} text-[color:var(--paper-ink-3)]`}>
          *
        </span>
      )}
    </span>
  );
}

function KpiTile({
  label,
  children,
  hint,
  className = "",
}: {
  label: string;
  children: React.ReactNode;
  hint?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`flex flex-col gap-1.5 border-r border-[color:var(--paper-line-2)] px-4 py-3 last:border-r-0 ${className}`}
    >
      <span className="font-mono text-[10px] font-semibold tracking-[0.09em] text-[color:var(--paper-ink-3)] uppercase">
        {label}
      </span>
      {children}
      {hint ? (
        <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
          {hint}
        </span>
      ) : null}
    </div>
  );
}

function TaskDetailHeader({
  task,
  onOpenTaskFiles,
}: {
  task: Task;
  onOpenTaskFiles: () => void;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-mono truncate text-[26px] font-semibold leading-[1.25] tracking-[-0.02em] text-[color:var(--paper-ink)]">
              {task.name}
            </h1>
            <Badge variant="outline" className="font-mono text-[11px]">
              v{task.current_version ?? "—"}
            </Badge>
          </div>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11.5px] text-[color:var(--paper-ink-3)]">
          {task.experiment_name ? (
            <>
              <span>experiment</span>
              <Link
                href={`/experiments/${encodeURIComponent(encodeURIComponent(task.experiment_id))}`}
                className="text-[color:var(--paper-ink-2)] underline-offset-2 hover:underline"
              >
                {task.experiment_name}
              </Link>
            </>
          ) : null}
          {task.github_username || task.user ? (
            <>
              <span aria-hidden>·</span>
              <span>by {task.github_username || task.user}</span>
            </>
          ) : null}
          {task.created_at ? (
            <>
              <span aria-hidden>·</span>
              <span title={new Date(task.created_at).toLocaleString()}>
                created {formatRelativeTime(task.created_at)}
              </span>
            </>
          ) : null}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Link href="/tasks">
          <Button
            type="button"
            variant="ghost"
            className="h-8 gap-1.5 rounded-[7px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px]"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            All tasks
          </Button>
        </Link>
        <Button
          type="button"
          onClick={onOpenTaskFiles}
          className="h-8 gap-1.5 rounded-[7px] px-3 text-[12px]"
        >
          <FileText className="h-3.5 w-3.5" />
          View task files
        </Button>
      </div>
    </div>
  );
}

type AggregateSummary = {
  trialCount: number;
  completed: number;
  failed: number;
  passCount: number;
  partialCount: number;
  failCount: number;
  harnessErrorCount: number;
  pendingCount: number;
  rewardSum: number;
  rewardTotal: number;
  costUsd: number;
  costTrialCount: number;
  costHasEstimated: boolean;
  costHasNative: boolean;
  lastRunAt: string | null;
};

const EMPTY_SUMMARY: AggregateSummary = {
  trialCount: 0,
  completed: 0,
  failed: 0,
  passCount: 0,
  partialCount: 0,
  failCount: 0,
  harnessErrorCount: 0,
  pendingCount: 0,
  rewardSum: 0,
  rewardTotal: 0,
  costUsd: 0,
  costTrialCount: 0,
  costHasEstimated: false,
  costHasNative: false,
  lastRunAt: null,
};

function summarizeTrials(trials: Trial[]): AggregateSummary {
  const s: AggregateSummary = { ...EMPTY_SUMMARY };
  for (const trial of trials) {
    s.trialCount += 1;
    if (trial.cost_usd != null) {
      s.costUsd += trial.cost_usd;
      s.costTrialCount += 1;
      if (trial.cost_is_estimated === true) s.costHasEstimated = true;
      else s.costHasNative = true;
    }
    if (trial.status === "success") s.completed += 1;
    else if (trial.status === "failed") s.failed += 1;

    if (trial.status === "success" && trial.reward != null) {
      s.rewardSum += trial.reward;
      s.rewardTotal += 1;
      if (trial.reward === 1) s.passCount += 1;
      else if (trial.reward === 0) s.failCount += 1;
      else s.partialCount += 1;
    } else if (trial.status === "failed") {
      s.harnessErrorCount += 1;
    } else if (trial.status !== "success") {
      s.pendingCount += 1;
    }

    const candidate = trial.finished_at || trial.started_at || trial.created_at;
    if (candidate && (s.lastRunAt == null || candidate > s.lastRunAt)) {
      s.lastRunAt = candidate;
    }
  }
  return s;
}

function summaryFromVersion(v: TaskVersionSummary): AggregateSummary {
  return {
    trialCount: v.trial_count,
    completed: v.completed_count,
    failed: v.failed_count,
    passCount: v.pass_count,
    partialCount: v.partial_count,
    failCount: v.fail_count,
    harnessErrorCount: 0,
    pendingCount: v.pending_count,
    rewardSum: v.reward_sum,
    rewardTotal: v.reward_total,
    costUsd: v.cost_usd,
    costTrialCount: v.cost_trial_count,
    costHasEstimated: v.cost_has_estimated,
    costHasNative: v.cost_has_native,
    lastRunAt: v.last_run_at ?? null,
  };
}

function VersionSwitcher({
  versions,
  selectedVersionId,
  onSelect,
}: {
  versions: TaskVersionSummary[];
  selectedVersionId: string;
  onSelect: (id: string) => void;
}) {
  if (versions.length === 0) return null;

  const totalTrials = versions.reduce((a, v) => a + v.trial_count, 0);
  const selected = versions.find((v) => v.id === selectedVersionId);

  const triggerLabel =
    selectedVersionId === ALL_VERSIONS_ID
      ? `All versions · ${totalTrials} trial${totalTrials === 1 ? "" : "s"}`
      : selected
        ? `v${selected.version}${selected.is_current ? " · current" : ""}`
        : "Select version";

  return (
    <Select value={selectedVersionId} onValueChange={onSelect}>
      <SelectTrigger className="font-mono h-8 w-[260px] rounded-[7px] border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-3 text-[12px] text-[color:var(--paper-ink)] hover:bg-[color:var(--paper-surface-2)]">
        <SelectValue placeholder="Select version">{triggerLabel}</SelectValue>
      </SelectTrigger>
      <SelectContent className="font-mono w-[360px]">
        <SelectItem value={ALL_VERSIONS_ID}>
          <div className="flex w-full flex-col gap-0.5 py-0.5">
            <span className="text-[12px] font-semibold text-[color:var(--paper-ink)]">
              All versions
            </span>
            <span className="text-[10.5px] text-[color:var(--paper-ink-3)]">
              {totalTrials} trial{totalTrials === 1 ? "" : "s"} across{" "}
              {versions.length} version{versions.length === 1 ? "" : "s"}
            </span>
          </div>
        </SelectItem>
        {versions.map((v) => {
          const label = v.is_current ? `v${v.version} · current` : `v${v.version}`;
          const cost =
            v.cost_trial_count > 0
              ? `${v.cost_has_estimated && !v.cost_has_native ? "~" : ""}${formatCostUsd(v.cost_usd)}`
              : "$0";
          return (
            <SelectItem key={v.id} value={v.id}>
              <div className="flex w-full flex-col gap-0.5 py-0.5">
                <span className="text-[12px] font-semibold text-[color:var(--paper-ink)]">
                  {label}
                </span>
                <span className="text-[10.5px] text-[color:var(--paper-ink-3)]">
                  {v.trial_count} trial{v.trial_count === 1 ? "" : "s"} · {cost}
                  {v.message ? ` · ${v.message}` : ""}
                </span>
              </div>
            </SelectItem>
          );
        })}
      </SelectContent>
    </Select>
  );
}

function VerdictBlock({ task }: { task: Task }) {
  if (!task.run_analysis && !task.verdict_status && !task.verdict) return null;

  const status = task.verdict_status;
  const verdict = task.verdict;
  const isPending =
    status === "running" || status === "pending" || status === "queued";
  const isFailed = status === "failed";

  let icon: React.ReactNode;
  let title: string;
  let toneClass: string;

  if (isPending) {
    icon = <Loader2 className="h-4 w-4 shrink-0 animate-spin text-blue-500" />;
    title = "Computing verdict...";
    toneClass = "border-[color:var(--paper-line)]";
  } else if (isFailed) {
    icon = <XCircle className="h-4 w-4 shrink-0 text-red-500" />;
    title = "Verdict failed";
    toneClass = "border-red-500/40 bg-red-500/[0.04]";
  } else if (verdict?.is_good === true) {
    icon = <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />;
    title = "Task is good";
    toneClass = "border-emerald-500/40 bg-emerald-500/[0.04]";
  } else if (verdict?.is_good === false) {
    icon = <AlertTriangle className="h-4 w-4 shrink-0 text-amber-500" />;
    title = "Needs review";
    toneClass = "border-amber-500/40 bg-amber-500/[0.04]";
  } else {
    icon = <Microscope className="h-4 w-4 shrink-0 text-slate-500" />;
    title = "Verdict pending";
    toneClass = "border-[color:var(--paper-line)]";
  }

  const detail =
    isFailed && task.verdict_error
      ? task.verdict_error
      : verdict?.is_good === false
        ? verdict.primary_issue ?? verdict.reasoning ?? null
        : verdict?.is_good === true
          ? verdict.reasoning ?? null
          : null;

  return (
    <div
      className={`flex items-start gap-2.5 rounded-[10px] border px-3 py-2 ${toneClass}`}
    >
      {icon}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-[12px] font-semibold text-[color:var(--paper-ink)]">
            {title}
          </span>
          {verdict?.confidence ? (
            <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
              · {verdict.confidence} confidence
            </span>
          ) : null}
        </div>
        {detail ? (
          <p className="font-mono mt-0.5 text-[11px] leading-snug text-[color:var(--paper-ink-2)]">
            {detail}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function TrialChip({
  trial,
  onClick,
}: {
  trial: Trial;
  onClick: () => void;
}) {
  const status = getMatrixStatus(trial.status, trial.reward, trial.error_message);
  const config = STATUS_CONFIG[status];
  const badgeLabel =
    status === "partial" ? formatPartialRewardBadgeValue(trial.reward) : null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          className={`flex h-[22px] w-[22px] items-center justify-center rounded-[4px] border font-mono font-semibold leading-none transition ${config.matrixClass} ${
            status === "partial" ? "text-[8px] tracking-[-0.03em]" : "text-[10px]"
          }`}
          style={getRewardStyle(trial.reward)}
          aria-label={`${trial.name} ${config.shortLabel}`}
        >
          {badgeLabel}
        </button>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          <div className="font-medium">{trial.name}</div>
          <div className="text-muted-foreground">{config.shortLabel}</div>
          {trial.reward !== null && (
            <div className="text-muted-foreground">
              Score {formatRewardValue(trial.reward)} (
              {formatRewardPercent(trial.reward)})
            </div>
          )}
          {trial.cost_usd != null && (
            <div className="text-muted-foreground">
              {trial.cost_is_estimated ? "~" : ""}
              {formatCostUsd(trial.cost_usd)}
            </div>
          )}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

function AgentCard({
  agentLabel,
  agent,
  model,
  trials,
  onTrialSelect,
}: {
  agentLabel: string;
  agent: string;
  model: string | null;
  trials: Trial[];
  onTrialSelect: (trial: Trial, trials: Trial[]) => void;
}) {
  const summary = useMemo(() => summarizeTrials(trials), [trials]);
  const scorePct =
    summary.rewardTotal > 0
      ? (summary.rewardSum / summary.rewardTotal) * 100
      : null;
  const avgCostUsd =
    summary.costTrialCount > 0 ? summary.costUsd / summary.costTrialCount : null;
  const avgDurationSec = useMemo(() => {
    let sum = 0;
    let count = 0;
    for (const t of trials) {
      const d = trialDurationSec(t);
      if (d != null) {
        sum += d;
        count += 1;
      }
    }
    return count > 0 ? sum / count : null;
  }, [trials]);
  const sortedTrials = useMemo(
    () =>
      [...trials].sort((a, b) => {
        const aTime = a.finished_at || a.started_at || a.created_at;
        const bTime = b.finished_at || b.started_at || b.created_at;
        return aTime < bTime ? 1 : aTime > bTime ? -1 : 0;
      }),
    [trials],
  );

  return (
    <div className="rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[color:var(--paper-line-2)] px-4 py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="font-mono text-[14px] font-semibold text-[color:var(--paper-ink)]">
            {agent}
          </span>
          {model ? (
            <Badge variant="outline" className="font-mono text-[11px]">
              {model}
            </Badge>
          ) : null}
          {agentLabel !== agent && (
            <span className="font-mono text-[10px] text-[color:var(--paper-ink-3)]">
              {agentLabel}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mono text-[11px] text-[color:var(--paper-ink-2)]">
          <span>
            <span className="text-[color:var(--paper-ink-3)]">trials</span>{" "}
            <span className="text-[color:var(--paper-ink)]">{summary.trialCount}</span>
          </span>
          <span>
            <span className="text-[color:var(--paper-ink-3)]">avg score</span>{" "}
            <span className="text-[color:var(--paper-ink)]">
              {scorePct != null
                ? `${scorePct.toFixed(0)}% (${summary.passCount}/${summary.rewardTotal})`
                : "—"}
            </span>
          </span>
          <span>
            <span className="text-[color:var(--paper-ink-3)]">total cost</span>{" "}
            <CostBadge
              cost={summary.costUsd}
              trialCount={summary.costTrialCount}
              hasEstimated={summary.costHasEstimated}
              hasNative={summary.costHasNative}
              size="sm"
            />
          </span>
          <span title="Mean cost per priced trial">
            <span className="text-[color:var(--paper-ink-3)]">avg cost</span>{" "}
            <span className="text-[color:var(--paper-ink)]">
              {avgCostUsd != null ? formatCostUsd(avgCostUsd) : "—"}
            </span>
          </span>
          <span title="Mean wall-clock duration (started_at → finished_at)">
            <span className="text-[color:var(--paper-ink-3)]">avg duration</span>{" "}
            <span className="text-[color:var(--paper-ink)]">
              {avgDurationSec != null ? formatDurationSec(avgDurationSec) : "—"}
            </span>
          </span>
          {summary.lastRunAt ? (
            <span title={new Date(summary.lastRunAt).toLocaleString()}>
              <span className="text-[color:var(--paper-ink-3)]">last run</span>{" "}
              <span className="text-[color:var(--paper-ink)]">
                {formatRelativeTime(summary.lastRunAt)}
              </span>
            </span>
          ) : null}
        </div>
      </div>
      <div className="px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          {sortedTrials.map((trial) => (
            <TrialChip
              key={trial.id}
              trial={trial}
              onClick={() => onTrialSelect(trial, sortedTrials)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

type DrawerState = {
  mode: "task" | "trial";
  trial: Trial | null;
  trialIndex: number | null;
  orderedTrials: Trial[];
  trialGroups: Array<{ agent: string; model: string | null; trials: Trial[] }>;
};

interface TaskDetailClientProps {
  taskId: string;
  initialDetail?: TaskDetailResponse | null;
  initialVersionId?: string | null;
}

export function TaskDetailClient({
  taskId,
  initialDetail,
  initialVersionId,
}: TaskDetailClientProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const swrKey = `/api/tasks/${encodeURIComponent(taskId)}/detail`;

  const { data, error, isLoading, mutate } = useSWR<TaskDetailResponse>(
    swrKey,
    fetcher,
    {
      refreshInterval: 30000,
      revalidateOnFocus: false,
      keepPreviousData: true,
      fallbackData: initialDetail ?? undefined,
    },
  );

  const detail = data ?? initialDetail ?? null;
  const task = detail?.task ?? null;
  const versions = useMemo(() => detail?.versions ?? [], [detail]);
  const totals = detail?.totals;

  const queryVersionId = searchParams.get("version") ?? initialVersionId ?? null;
  const defaultVersionId =
    task?.current_version_id ?? versions[0]?.id ?? ALL_VERSIONS_ID;
  const selectedVersionId = useMemo(() => {
    if (!queryVersionId) return defaultVersionId;
    if (queryVersionId === ALL_VERSIONS_ID) return ALL_VERSIONS_ID;
    return versions.some((v) => v.id === queryVersionId)
      ? queryVersionId
      : defaultVersionId;
  }, [queryVersionId, defaultVersionId, versions]);

  const handleSelectVersion = useCallback(
    (id: string) => {
      const next = new URLSearchParams(searchParams.toString());
      if (id === defaultVersionId) {
        next.delete("version");
      } else {
        next.set("version", id);
      }
      const qs = next.toString();
      router.replace(qs ? `?${qs}` : `?`, { scroll: false });
    },
    [searchParams, router, defaultVersionId],
  );

  const trialsForVersion = useMemo(() => {
    if (!task?.trials) return [] as Trial[];
    if (selectedVersionId === ALL_VERSIONS_ID) return task.trials;
    return task.trials.filter((t) => t.task_version_id === selectedVersionId);
  }, [task, selectedVersionId]);

  const selectedVersion = versions.find((v) => v.id === selectedVersionId);
  const versionSummary: AggregateSummary = useMemo(() => {
    if (selectedVersionId === ALL_VERSIONS_ID) {
      return summarizeTrials(task?.trials ?? []);
    }
    if (selectedVersion) return summaryFromVersion(selectedVersion);
    return summarizeTrials(trialsForVersion);
  }, [selectedVersion, selectedVersionId, task, trialsForVersion]);

  const tasksForGrouping = useMemo<Task[]>(
    () =>
      task
        ? [
            {
              ...task,
              trials: trialsForVersion,
            },
          ]
        : [],
    [task, trialsForVersion],
  );

  const { agentSummaries, modelScopedAgents } = useMemo(
    () => buildExperimentAgentSummaries(tasksForGrouping),
    [tasksForGrouping],
  );

  const trialsByAgentKey = useMemo(() => {
    const map = new Map<string, Trial[]>();
    for (const trial of trialsForVersion) {
      const key = getExperimentAgentKey(trial, modelScopedAgents);
      const existing = map.get(key) ?? [];
      existing.push(trial);
      map.set(key, existing);
    }
    return map;
  }, [trialsForVersion, modelScopedAgents]);

  const trialGroups = useMemo(
    () =>
      agentSummaries.map((summary) => {
        const trials = trialsByAgentKey.get(summary.key) ?? [];
        return {
          agent: summary.key,
          model: summary.model,
          trials,
        };
      }),
    [agentSummaries, trialsByAgentKey],
  );

  const orderedTrials = useMemo(() => {
    const out: Trial[] = [];
    for (const group of trialGroups) out.push(...group.trials);
    return out;
  }, [trialGroups]);

  const [drawer, setDrawer] = useState<DrawerState | null>(null);
  const [drawerShowTask, setDrawerShowTask] = useState(true);
  const [drawerShowTrial, setDrawerShowTrial] = useState(true);

  const handleSelectTrial = useCallback(
    (trial: Trial) => {
      const trialIndex = orderedTrials.findIndex((t) => t.id === trial.id);
      setDrawer({
        mode: "trial",
        trial,
        trialIndex: trialIndex >= 0 ? trialIndex : null,
        orderedTrials,
        trialGroups,
      });
    },
    [orderedTrials, trialGroups],
  );

  const handleOpenTaskFiles = useCallback(() => {
    setDrawer({
      mode: "task",
      trial: null,
      trialIndex: null,
      orderedTrials,
      trialGroups,
    });
  }, [orderedTrials, trialGroups]);

  const handleNavigateToTrial = useCallback(
    (trial: Trial, trialIndex: number) => {
      setDrawer((prev) =>
        prev
          ? { ...prev, mode: "trial", trial, trialIndex }
          : prev,
      );
    },
    [],
  );

  const handleRerun = useCallback(() => {
    void mutate();
  }, [mutate]);

  const versionScopedScorePct =
    versionSummary.rewardTotal > 0
      ? (versionSummary.rewardSum / versionSummary.rewardTotal) * 100
      : null;

  if (error && !detail) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Failed to load task</AlertTitle>
        <AlertDescription>
          {error instanceof Error ? error.message : "Unknown error"}
        </AlertDescription>
      </Alert>
    );
  }

  if (!detail || !task) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-72" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  const versionLabel =
    selectedVersionId === ALL_VERSIONS_ID
      ? "All versions"
      : selectedVersion
        ? `v${selectedVersion.version}${selectedVersion.is_current ? " · current" : ""}`
        : "Selected version";

  return (
    <TooltipProvider>
      <div className="space-y-4">
        <TaskDetailHeader task={task} onOpenTaskFiles={handleOpenTaskFiles} />

        <VerdictBlock task={task} />

        <div className="grid grid-cols-2 overflow-hidden rounded-[10px] border border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] md:grid-cols-5">
          <KpiTile
            label="Total spent (all versions)"
            hint={
              totals && totals.cost_trial_count > 0
                ? `${totals.cost_trial_count} of ${totals.total_trials} trials priced`
                : totals && totals.total_trials > 0
                  ? `${totals.total_trials} trials, no cost data`
                  : "no trials yet"
            }
          >
            <CostBadge
              cost={totals?.cost_usd ?? 0}
              trialCount={totals?.cost_trial_count ?? 0}
              hasEstimated={totals?.cost_has_estimated ?? false}
              hasNative={totals?.cost_has_native ?? false}
              size="lg"
            />
          </KpiTile>
          <KpiTile
            label={`Spent on ${versionLabel}`}
            hint={
              versionSummary.costTrialCount > 0
                ? `${versionSummary.costTrialCount} trial${
                    versionSummary.costTrialCount === 1 ? "" : "s"
                  }`
                : "no cost data"
            }
          >
            <CostBadge
              cost={versionSummary.costUsd}
              trialCount={versionSummary.costTrialCount}
              hasEstimated={versionSummary.costHasEstimated}
              hasNative={versionSummary.costHasNative}
              size="lg"
            />
          </KpiTile>
          <KpiTile
            label="Trials"
            hint={`${versionSummary.completed} succeeded · ${versionSummary.failed} failed`}
          >
            <span className="font-display flex items-baseline gap-2 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
              {versionSummary.trialCount}
            </span>
          </KpiTile>
          <KpiTile
            label="Avg score"
            hint={
              versionSummary.rewardTotal > 0
                ? `${versionSummary.passCount} pass · ${versionSummary.partialCount} partial · ${versionSummary.failCount} fail`
                : "no scored trials"
            }
          >
            <span className="font-display flex items-baseline gap-2 text-[26px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
              {versionScopedScorePct != null
                ? `${versionScopedScorePct.toFixed(1)}%`
                : "—"}
              {versionSummary.rewardTotal > 0 ? (
                <span
                  className="font-mono text-[12px] text-[color:var(--paper-ink-3)]"
                  title={`${versionSummary.passCount} of ${versionSummary.rewardTotal} scored trials passed (reward = 1)`}
                >
                  {versionSummary.passCount}/{versionSummary.rewardTotal} pass
                </span>
              ) : null}
            </span>
          </KpiTile>
          <KpiTile
            label="Last run"
            hint={
              versionSummary.lastRunAt
                ? new Date(versionSummary.lastRunAt).toLocaleString()
                : undefined
            }
          >
            <span className="font-display flex items-baseline gap-2 text-[20px] leading-none font-medium tracking-[-0.02em] text-[color:var(--paper-ink)]">
              {versionSummary.lastRunAt
                ? formatRelativeTime(versionSummary.lastRunAt)
                : "—"}
            </span>
          </KpiTile>
        </div>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[10px] font-semibold tracking-[0.09em] text-[color:var(--paper-ink-3)] uppercase">
              Version
            </span>
            {isLoading ? (
              <Loader2 className="h-3 w-3 animate-spin text-[color:var(--paper-ink-3)]" />
            ) : null}
          </div>
          <VersionSwitcher
            versions={versions}
            selectedVersionId={selectedVersionId}
            onSelect={handleSelectVersion}
          />
        </div>

        <div className="space-y-3">
          <div className="flex items-baseline justify-between">
            <h2 className="font-mono text-[12px] font-semibold tracking-[0.06em] text-[color:var(--paper-ink-2)] uppercase">
              Agents
            </h2>
            <span className="font-mono text-[10.5px] text-[color:var(--paper-ink-3)]">
              {agentSummaries.length} agent
              {agentSummaries.length === 1 ? "" : "s"} · {trialsForVersion.length} trial
              {trialsForVersion.length === 1 ? "" : "s"}
            </span>
          </div>
          {agentSummaries.length === 0 ? (
            <div className="rounded-[10px] border border-dashed border-[color:var(--paper-line)] bg-[color:var(--paper-surface)] px-4 py-10 text-center text-[12px] text-[color:var(--paper-ink-3)]">
              No trials for this version yet.
            </div>
          ) : (
            agentSummaries.map((summary) => {
              const trials = trialsByAgentKey.get(summary.key) ?? [];
              return (
                <AgentCard
                  key={summary.key}
                  agentLabel={summary.label}
                  agent={summary.agent}
                  model={summary.model}
                  trials={trials}
                  onTrialSelect={handleSelectTrial}
                />
              );
            })
          )}
        </div>

        {drawer && (
          <UnifiedDrawerWrapper
            open={true}
            onOpenChange={(open) => !open && setDrawer(null)}
            mode={drawer.mode}
            showTask={drawerShowTask}
            showTrial={drawerShowTrial}
            onShowTaskChange={setDrawerShowTask}
            onShowTrialChange={setDrawerShowTrial}
            sideBySideLeft={
              <TaskFilesPanel
                isOpen={true}
                onClose={() => {}}
                taskId={null}
                filesUrl={`/api/tasks/${task.id}/files`}
                apiBaseUrl="/api"
                contentOnly={true}
              />
            }
            taskContent={
              <TaskFilesPanel
                isOpen={true}
                onClose={() => setDrawer(null)}
                taskId={task.id}
                task={task}
                onRetryComplete={handleRerun}
                allowRetry={true}
                onNavigateToFirstTrial={
                  drawer.trialGroups.length > 0 &&
                  drawer.trialGroups[0].trials.length > 0
                    ? () => {
                        const firstTrial = drawer.trialGroups[0].trials[0];
                        handleSelectTrial(firstTrial);
                      }
                    : undefined
                }
                apiBaseUrl="/api"
                contentOnly={true}
              />
            }
            renderTrial={(paneAction) =>
              drawer.trial && (
                <TrialDetailPanel
                  isOpen={true}
                  onClose={() => setDrawer(null)}
                  trial={drawer.trial}
                  task={task}
                  orderedTrials={drawer.orderedTrials}
                  trialIndex={drawer.trialIndex}
                  trialGroups={drawer.trialGroups}
                  onNavigate={handleNavigateToTrial}
                  onNavigateToTask={() =>
                    setDrawer((prev) =>
                      prev
                        ? { ...prev, mode: "task", trial: null, trialIndex: null }
                        : prev,
                    )
                  }
                  onRetry={handleRerun}
                  allowRetry={true}
                  apiBaseUrl="/api"
                  contentOnly={true}
                  paneAction={paneAction}
                />
              )
            }
          />
        )}
      </div>
    </TooltipProvider>
  );
}
